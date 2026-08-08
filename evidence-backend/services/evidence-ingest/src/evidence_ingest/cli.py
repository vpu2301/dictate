"""Operator CLI (spec §1): runs the pipeline locally, no service required.

    uv run evidence-ingest ingest file <path> --kind pdf [--tenant global] [--set k=v ...]
    uv run evidence-ingest ingest dir <path> --kind pdf [...]
    uv run evidence-ingest snapshot <label> [--tenant global]
    uv run evidence-ingest retractions <feed.csv> [--tenant global]
    uv run evidence-ingest stats [--tenant global]

`--tenant global` (default) targets the reserved global-corpus tenant; pass a
tenant UUID for tenant-scoped ingestion (S12 portal path)."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from pathlib import Path
from uuid import UUID

from db import tenant_connection

from evidence_ingest.constants import GLOBAL_TENANT
from evidence_ingest.domain import repository
from evidence_ingest.domain.pipeline import PermanentJobError, run_job
from evidence_ingest.domain.retractions import process_feed
from evidence_ingest.domain.snapshots import SnapshotDirtyError, build_snapshot
from evidence_ingest.main_deps import build_state, teardown_state

_KIND_BY_SUFFIX = {
    ".pdf": "pdf",
    ".xml": "pmc_xml",
    ".html": "html_guideline",
    ".htm": "html_guideline",
    ".md": "markdown",
    ".docx": "docx",
}


def _tenant(value: str) -> UUID:
    return GLOBAL_TENANT if value == "global" else UUID(value)


def _overrides(pairs: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            raise SystemExit(f"--set expects key=value, got {pair!r}")
        result[key.strip()] = value.strip()
    return result


async def _ingest_paths(
    paths: list[Path], *, kind: str | None, tenant_id: UUID, overrides: dict[str, str]
) -> int:
    state = await build_state()
    failures = 0
    try:
        for path in paths:
            file_kind = kind or _KIND_BY_SUFFIX.get(path.suffix.lower())
            if file_kind is None:
                print(f"SKIP {path} (unknown format)")
                continue
            source_uri = f"file://{path.resolve()}"
            idempotence_key = hashlib.sha256(
                (source_uri + hashlib.sha256(path.read_bytes()).hexdigest()).encode()
            ).hexdigest()
            async with tenant_connection(state.app_pool, tenant_id) as conn:
                job_id = await repository.create_job(
                    conn,
                    tenant_id=tenant_id,
                    source_uri=source_uri,
                    kind=file_kind,
                    idempotence_key=idempotence_key,
                    metadata_overrides=overrides,
                )
            if job_id is None:
                async with tenant_connection(state.app_pool, tenant_id) as conn:
                    existing = await conn.fetchrow(
                        "SELECT id, state FROM ingest_jobs"
                        " WHERE tenant_id = $1 AND idempotence_key = $2",
                        tenant_id,
                        idempotence_key,
                    )
                if existing is None or existing["state"] in ("done", "dead", "quarantined"):
                    print(
                        f"       NOOP {path} (already ingested: "
                        f"{existing['state'] if existing else 'unknown'})"
                    )
                    continue
                # Interrupted job: resume from its recorded stage.
                job_id = existing["id"]
                print(f"     RESUME {path} (job {job_id} at {existing['state']})")
            started = time.monotonic()
            try:
                final_state = await run_job(state.pipeline, job_id=job_id, tenant_id=tenant_id)
                elapsed = time.monotonic() - started
                print(f"{final_state.upper():>11} {path} ({elapsed:.1f}s, job {job_id})")
                if final_state == "dead":
                    failures += 1
            except PermanentJobError as exc:
                failures += 1
                print(f"       DEAD {path}: {exc}")
            except Exception as exc:  # noqa: BLE001 — one file must not kill the run
                failures += 1
                print(
                    f"      RETRY {path}: {type(exc).__name__}: {exc} "
                    "(transient; job resumable, re-run to continue)"
                )
    finally:
        await teardown_state(state)
    return failures


async def _snapshot(label: str, tenant_id: UUID) -> int:
    state = await build_state()
    try:
        result = await build_snapshot(
            pool=state.app_pool,
            audit_writer=state.audit_writer,
            tenant_id=tenant_id,
            label=label,
        )
        print(
            f"snapshot {result.snapshot_id} frozen: {result.member_count} member version(s), "
            f"{result.license_excluded} license-excluded"
        )
        return 0
    except SnapshotDirtyError as exc:
        print(f"snapshot_dirty: {exc.pending} pending job(s)", file=sys.stderr)
        return 1
    finally:
        await teardown_state(state)


async def _retractions(feed: Path, tenant_id: UUID) -> int:
    state = await build_state()
    try:
        result = await process_feed(
            pool=state.app_pool,
            audit_writer=state.audit_writer,
            lexical=state.lexical,
            tenant_id=tenant_id,
            feed_raw=feed.read_bytes(),
        )
        print(f"flagged {result.flagged}: {', '.join(result.canonical_ids) or '—'}")
        return 0
    finally:
        await teardown_state(state)


async def _quarantine_list(tenant_id: UUID) -> int:
    state = await build_state()
    try:
        async with tenant_connection(state.app_pool, tenant_id) as conn:
            rows = await conn.fetch(
                "SELECT id, job_id, reason, patterns, created_at FROM quarantine"
                " WHERE decision IS NULL ORDER BY created_at"
            )
        for row in rows:
            print(f"{row['id']}  job={row['job_id']}  {row['reason']}")
            for hit in json.loads(row["patterns"]):
                print(f"    [{hit['pattern']}] …{hit['excerpt'][:80]}…")
        if not rows:
            print("no open quarantine entries")
        return 0
    finally:
        await teardown_state(state)


async def _quarantine_decide(quarantine_id: UUID, decision: str, tenant_id: UUID) -> int:
    """Operator decision path (global corpus); tenant corpora use the HTTP
    endpoint where the reviewer's identity comes from their token."""
    state = await build_state()
    try:
        async with tenant_connection(state.app_pool, tenant_id) as conn:
            row = await repository.decide_quarantine(
                conn,
                quarantine_id=quarantine_id,
                reviewed_by=UUID("00000000-0000-0000-0000-000000000000"),
                decision=decision,
            )
            if row is None:
                print("not found or already decided", file=sys.stderr)
                return 1
            job_id: UUID = row["job_id"]
            next_state = "chunking" if decision == "approved" else "dead"
            await repository.set_job_state(conn, job_id=job_id, state=next_state)
        await state.audit_writer.write_event(
            tenant_id=tenant_id,
            kind="evidence.quarantine_decided",
            target_kind="quarantine",
            target_id=quarantine_id,
            payload={"decision": decision, "job_id": str(job_id), "via": "operator-cli"},
        )
        if decision == "approved":
            final_state = await run_job(state.pipeline, job_id=job_id, tenant_id=tenant_id)
            print(f"approved; job resumed → {final_state}")
        else:
            print("rejected; job dead")
        return 0
    finally:
        await teardown_state(state)


async def _stats(tenant_id: UUID) -> int:
    state = await build_state()
    try:
        async with tenant_connection(state.app_pool, tenant_id) as conn:
            stats = await repository.corpus_stats(conn, tenant_id=tenant_id)
        print(json.dumps(stats, indent=2, default=str))
        return 0
    finally:
        await teardown_state(state)


def main() -> None:
    parser = argparse.ArgumentParser(prog="evidence-ingest")
    parser.add_argument("--tenant", default="global", help="'global' or a tenant UUID")
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="ingest a file or directory")
    ingest.add_argument("what", choices=["file", "dir"])
    ingest.add_argument("path", type=Path)
    ingest.add_argument("--kind", choices=sorted(set(_KIND_BY_SUFFIX.values())), default=None)
    ingest.add_argument(
        "--set",
        dest="overrides",
        action="append",
        default=[],
        help="metadata override key=value (repeatable)",
    )

    snapshot = sub.add_parser("snapshot", help="freeze an immutable corpus snapshot")
    snapshot.add_argument("label")

    retractions = sub.add_parser("retractions", help="process a retraction feed CSV")
    retractions.add_argument("feed", type=Path)

    quarantine = sub.add_parser("quarantine", help="review quarantined documents")
    quarantine_sub = quarantine.add_subparsers(dest="quarantine_command", required=True)
    quarantine_sub.add_parser("list")
    decide = quarantine_sub.add_parser("decide")
    decide.add_argument("quarantine_id", type=UUID)
    decide.add_argument("decision", choices=["approved", "rejected"])

    sub.add_parser("stats", help="corpus statistics")

    args = parser.parse_args()
    tenant_id = _tenant(args.tenant)

    if args.command == "ingest":
        if args.what == "file":
            paths = [args.path]
        else:
            paths = sorted(p for p in args.path.rglob("*") if p.suffix.lower() in _KIND_BY_SUFFIX)
            if not paths:
                raise SystemExit(f"no ingestable files under {args.path}")
        failures = asyncio.run(
            _ingest_paths(
                paths, kind=args.kind, tenant_id=tenant_id, overrides=_overrides(args.overrides)
            )
        )
        raise SystemExit(1 if failures else 0)
    if args.command == "snapshot":
        raise SystemExit(asyncio.run(_snapshot(args.label, tenant_id)))
    if args.command == "retractions":
        raise SystemExit(asyncio.run(_retractions(args.feed, tenant_id)))
    if args.command == "quarantine":
        if args.quarantine_command == "list":
            raise SystemExit(asyncio.run(_quarantine_list(tenant_id)))
        raise SystemExit(
            asyncio.run(_quarantine_decide(args.quarantine_id, args.decision, tenant_id))
        )
    if args.command == "stats":
        raise SystemExit(asyncio.run(_stats(tenant_id)))


if __name__ == "__main__":
    main()
