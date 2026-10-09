"""corpus-forge CLI — mine / gaps / import / generate / jury / review /
promote / release / prompts.

Operator tool, not a service: runs offline against MDX_CORPUS_DSN and never
serves traffic. Every stage writes to `corpus_candidates`; only `promote`
touches the live corpus; only `release` publishes (ADR-0043).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import logging
import sys
import uuid
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg

from audit import AuditWriter
from corpus_forge import audit_kinds
from corpus_forge.adapters import candidates as cand_db
from corpus_forge.adapters import languagetool, mining
from corpus_forge.adapters.llm import (
    ExternalAnthropicClient,
    LocalLlamaClient,
    LocalOllamaClient,
    build_local_client,
)
from corpus_forge.config import Settings, load_settings
from corpus_forge.domain import generate as gen
from corpus_forge.domain import importers
from corpus_forge.domain import prompts as prompt_dom
from corpus_forge.domain.dedupe import dedupe_batch
from corpus_forge.domain.fluency import build_fluency_filter
from corpus_forge.domain.jury import (
    JuryLLMClient,
    run_jury,
)
from corpus_forge.domain.kanon import MinedStats, passes_k_anonymity
from corpus_forge.domain.normalize import dedupe_key
from corpus_forge.domain.perimeter import assert_in_perimeter_url
from corpus_forge.domain.phrasing import load_template_set, phrasify
from corpus_forge.domain.pii import contains_pii
from corpus_forge.domain.quota import check_quota, load_quota
from corpus_forge.domain.release import ReleaseRow, build_manifest
from corpus_forge.domain.validate import validate_release_csv
from corpus_risk import RiskFlagger, default_flagger, load_wordlist, route_tier
from db import create_pool

logger = logging.getLogger("corpus_forge")

GLOBAL_TENANT = UUID("00000000-0000-0000-0000-000000000000")
SEEDS_ROOT = Path("infra/seeds/corpus")


# ── shared plumbing ──────────────────────────────────────────────────


async def _connect(settings: Settings) -> asyncpg.Pool:
    return await create_pool(settings.corpus_dsn, application_name="corpus-forge")


async def _audit(settings: Settings, kind: str, payload: dict[str, Any]) -> None:
    """Corpus events chain under the reserved global tenant. Optional in dev:
    without an audit DSN the event is logged, not chained."""
    if not settings.audit_dsn:
        logger.warning("audit DSN unset — %s not chained: %s", kind, payload)
        return
    pool = await create_pool(settings.audit_dsn, application_name="corpus-forge-audit")
    try:
        writer = AuditWriter(pool)
        await writer.write_event(tenant_id=GLOBAL_TENANT, kind=kind, payload=payload)
    finally:
        await pool.close()


def _local_client(settings: Settings) -> LocalLlamaClient | LocalOllamaClient:
    """The ONLY constructor for the in-perimeter backend: asserts the URL is
    private before any client exists (refuse to start, don't warn)."""
    assert_in_perimeter_url(settings.llm_base_url, purpose="in-perimeter LLM backend")
    return build_local_client(
        backend=settings.llm_backend, base_url=settings.llm_base_url, model=settings.llm_model
    )


def _build_flagger() -> RiskFlagger:
    """The lexicons now travel with corpus_risk, so the CLI and the console's
    POST /corpus/candidates flag identically — and neither depends on being
    run from a repo checkout."""
    return default_flagger()


def _draft_from_phrase(
    *,
    phrase: str,
    language: str,
    source_kind: str,
    source_ref: str,
    flagger: RiskFlagger,
    specialty: str | None = None,
    section_hint: str | None = None,
    batch_id: UUID | None = None,
    report: dict[str, Any] | None = None,
    validators_passed: bool = True,
) -> cand_db.CandidateDraft | None:
    """Validation → risk flags → tier, or None when PII forces a drop."""
    if contains_pii(phrase):
        return None
    flags = tuple(flagger.flags(phrase))
    tier = route_tier(
        source_kind=source_kind, risk_flags=flags, validators_passed=validators_passed
    )
    return cand_db.CandidateDraft(
        phrase=phrase,
        dedupe_key=dedupe_key(phrase),
        language=language,
        source_kind=source_kind,
        source_ref=source_ref,
        specialty=specialty,
        section_hint=section_hint,
        generation_batch_id=batch_id,
        risk_flags=flags,
        tier=tier,
        validator_report=report or {},
    )


# ── subcommands ──────────────────────────────────────────────────────


async def cmd_mine(settings: Settings, args: argparse.Namespace) -> int:
    run_id = str(uuid.uuid4())
    flagger = _build_flagger()
    pool = await _connect(settings)
    try:
        async with pool.acquire() as conn:
            grams = await mining.mine_ngrams(
                conn,
                min_authors=settings.k_min_authors,
                min_tenants=settings.k_min_tenants,
                min_frequency=settings.min_frequency,
                limit=args.limit,
            )
            drafts: list[cand_db.CandidateDraft] = []
            dropped_pii = 0
            dropped_kanon = 0
            for g in grams:
                stats = MinedStats(g.frequency, g.distinct_authors, g.distinct_tenants)
                if not passes_k_anonymity(
                    stats,
                    min_authors=settings.k_min_authors,
                    min_tenants=settings.k_min_tenants,
                ):  # defense in depth over the SQL HAVING
                    dropped_kanon += 1
                    continue
                draft = _draft_from_phrase(
                    phrase=g.gram,
                    language=args.language,
                    source_kind="mined",
                    source_ref=f"mining:{run_id}",
                    flagger=flagger,
                    report={
                        "frequency": g.frequency,
                        "distinct_authors": g.distinct_authors,
                        "distinct_tenants": g.distinct_tenants,
                    },
                )
                if draft is None:
                    dropped_pii += 1
                    continue
                drafts.append(draft)
            inserted = await cand_db.upsert_candidates(conn, drafts)
    finally:
        await pool.close()
    await _audit(
        settings,
        audit_kinds.MINING_RUN,
        {
            "run_id": run_id,
            "query_sha256": mining.mining_query_sha256(),
            "grams": len(grams),
            "inserted": inserted,
            "dropped_pii": dropped_pii,
            "dropped_kanon": dropped_kanon,
            "k_min_authors": settings.k_min_authors,
            "k_min_tenants": settings.k_min_tenants,
        },
    )
    print(
        f"mine: {len(grams)} grams from SQL → {inserted} new candidates "
        f"(pii-dropped {dropped_pii}, k-anon re-check dropped {dropped_kanon})"
    )
    return 0


async def cmd_gaps(settings: Settings, args: argparse.Namespace) -> int:
    pool = await _connect(settings)
    try:
        async with pool.acquire() as conn:
            gaps = await mining.telemetry_gaps(conn, limit=args.limit)
    finally:
        await pool.close()
    for g in gaps:
        print(f"{g.impressions}\t{g.accepts}\t{'timeout-only' if g.all_timeouts else ''}\t{g.prefix}")
    print(f"# {len(gaps)} zero-accept prefixes (work queue, ranked by volume)", file=sys.stderr)
    return 0


async def cmd_import(settings: Settings, args: argparse.Namespace) -> int:
    raw = importers.load_dataset(Path(args.file))
    source_ref = importers.dataset_source_ref(args.dataset, args.dataset_version, raw)
    if args.dataset == "drlz":
        terms = importers.parse_drlz_csv(raw)
    else:
        terms = importers.parse_term_lines(raw, section=args.section)

    template_path = SEEDS_ROOT / "phrasing" / args.language
    flagger = _build_flagger()
    drafts: list[cand_db.CandidateDraft] = []
    for term in terms:
        tpl_file = template_path / f"{term.section}.yaml"
        if not tpl_file.exists():
            continue
        for phrase in phrasify(term.text, load_template_set(tpl_file)):
            draft = _draft_from_phrase(
                phrase=phrase,
                language=args.language,
                source_kind="terminology",
                source_ref=source_ref,
                flagger=flagger,
                specialty=args.specialty,
                section_hint=term.section,
            )
            if draft is not None:
                drafts.append(draft)

    pool = await _connect(settings)
    try:
        async with pool.acquire() as conn:
            accepted = await cand_db.fetch_accepted_phrases(conn, language=args.language)
            unique = dedupe_batch([d.phrase for d in drafts], against=accepted)
            keep = set(unique)
            drafts = [d for d in drafts if d.phrase in keep]
            inserted = await cand_db.upsert_candidates(conn, drafts)
    finally:
        await pool.close()
    await _audit(
        settings,
        audit_kinds.CANDIDATE_GENERATED,
        {"source": args.dataset, "source_ref": source_ref, "terms": len(terms), "inserted": inserted},
    )
    print(f"import {args.dataset}: {len(terms)} terms → {inserted} new candidates ({source_ref})")
    return 0


async def cmd_generate(settings: Settings, args: argparse.Namespace) -> int:
    batch_id = uuid.uuid4()
    client = _local_client(settings)
    flagger = _build_flagger()
    fluency = build_fluency_filter(settings.kenlm_model_path)
    pool = await _connect(settings)
    try:
        async with pool.acquire() as conn:
            avoid = await cand_db.fetch_accepted_phrases(conn, language=args.language)
            prompt = gen.build_generation_prompt(
                language=args.language,
                specialty=args.specialty,
                section=args.section,
                seed_terms=args.seed_term or [],
                avoid_phrases=avoid,
                target_count=args.count,
            )
            raw = await client.complete(prompt)
            parsed = gen.parse_generation_batch(
                raw, language=args.language, specialty=args.specialty, section=args.section
            )
            fluent = [p.phrase for p in parsed.kept if fluency.keep(p.phrase)]
            unique = dedupe_batch(fluent, against=avoid)
            drafts = []
            for phrase in unique:
                draft = _draft_from_phrase(
                    phrase=phrase,
                    language=args.language,
                    source_kind="generated",
                    source_ref=f"generation:{settings.llm_model}:{batch_id}",
                    flagger=flagger,
                    specialty=args.specialty,
                    section_hint=args.section,
                    batch_id=batch_id,
                )
                if draft is not None:
                    drafts.append(draft)
            inserted = await cand_db.upsert_candidates(conn, drafts)
    finally:
        await client.aclose()
        await pool.close()
    await _audit(
        settings,
        audit_kinds.CANDIDATE_GENERATED,
        {
            "batch_id": str(batch_id),
            "model": settings.llm_model,
            "requested": args.count,
            "parsed": len(parsed.kept),
            "dropped_malformed": parsed.dropped_malformed,
            "dropped_gates": parsed.dropped_gates,
            "fluency_filter": fluency.name,
            "inserted": inserted,
        },
    )
    print(
        f"generate: requested {args.count} → parsed {parsed.kept and len(parsed.kept) or 0}, "
        f"fluent {len(fluent)}, unique {len(unique)}, inserted {inserted} (batch {batch_id})"
    )
    return 0


def _jury_prompts(phrase: str, language: str, specialty: str | None, section: str | None, version: str) -> list[str]:
    prompt_dir = SEEDS_ROOT / "jury" / version
    rendered: list[str] = []
    for f in sorted(prompt_dir.glob("*.md")):
        rendered.append(
            f.read_text(encoding="utf-8").format(
                phrase=phrase,
                language=language,
                specialty=specialty or "general",
                section=section or "unspecified",
            )
        )
    if not rendered:
        raise FileNotFoundError(f"no jury prompts under {prompt_dir}")
    return rendered


async def cmd_jury(settings: Settings, args: argparse.Namespace) -> int:
    """Jury pass over unrouted candidates of one tier (1 or 2). Auto-accept
    on tier 1 requires --calibrated (the ≤2% false-accept gate, ADR-0044 §3)."""
    if args.tier == 1 and not args.calibrated:
        print(
            "jury: tier-1 auto-accept requires --calibrated "
            "(docs/eval/sprint-21-jury-calibration.md must show false-accept ≤ 2%)",
            file=sys.stderr,
        )
        return 2

    client: JuryLLMClient
    if args.external:
        if not settings.external_api_key:
            print("jury: --external needs MDX_CORPUS_EXTERNAL_API_KEY", file=sys.stderr)
            return 2
        client = ExternalAnthropicClient(
            api_key=settings.external_api_key, model=settings.external_model
        )
    else:
        client = _local_client(settings)

    engine = f"jury:{client.model_name}:{settings.jury_prompt_version}"
    pool = await _connect(settings)
    decided = disagreements = escalated = 0
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, phrase, language, specialty, section_hint, source_kind
                FROM corpus_candidates
                WHERE review_state = 'candidate' AND tier = $1 AND tenant_id IS NULL
                ORDER BY created_at
                LIMIT $2
                """,
                args.tier,
                args.limit,
            )
            for row in rows:
                result = await run_jury(
                    client=client,
                    source_kind=str(row["source_kind"]),
                    prompts=_jury_prompts(
                        str(row["phrase"]),
                        str(row["language"]),
                        row["specialty"],
                        row["section_hint"],
                        settings.jury_prompt_version,
                    ),
                    prompt_version=settings.jury_prompt_version,
                )
                votes_json = json.dumps([v.model_dump() for v in result.votes], ensure_ascii=False)
                if result.disagreement:
                    disagreements += 1
                    await _audit(
                        settings,
                        audit_kinds.JURY_DISAGREEMENT,
                        {"candidate_id": str(row["id"]), "engine": engine},
                    )
                if args.tier == 1:
                    accepted: bool | None = result.unanimous_accept
                    escalate = None
                else:
                    if result.disagreement:
                        accepted, escalate = None, 3  # split tier-2 vote → human
                        escalated += 1
                    else:
                        accepted, escalate = result.majority_accept, None
                await cand_db.record_jury_outcome(
                    conn,
                    candidate_id=row["id"],
                    votes_json=votes_json,
                    accepted=accepted,
                    escalate_to_tier=escalate,
                    review_engine=engine,
                )
                if accepted is not None:
                    decided += 1
                    if accepted:
                        await _audit(
                            settings,
                            audit_kinds.AUTO_ACCEPTED,
                            {"candidate_id": str(row["id"]), "engine": engine, "tier": args.tier},
                        )
    finally:
        await client.aclose()
        await pool.close()
    print(
        f"jury(tier {args.tier}, {engine}): {len(rows)} candidates, {decided} decided, "
        f"{escalated} escalated to tier 3, {disagreements} disagreements"
    )
    return 0


async def cmd_review(settings: Settings, args: argparse.Namespace) -> int:
    """Record one human decision (the keyboard-driven review UI's backend;
    also usable directly for spot audits)."""
    pool = await _connect(settings)
    try:
        async with pool.acquire() as conn:
            await cand_db.record_review(
                conn,
                candidate_id=UUID(args.candidate_id),
                reviewer_id=UUID(args.reviewer),
                decision=args.decision,
                edited_text=args.edited_text,
                latency_ms=args.latency_ms,
                review_engine="human",
            )
    finally:
        await pool.close()
    await _audit(
        settings,
        audit_kinds.CANDIDATE_REVIEWED,
        {
            "candidate_id": args.candidate_id,
            "decision": args.decision,
            "latency_ms": args.latency_ms,
        },
    )
    print(f"review: {args.candidate_id} → {args.decision}")
    return 0


async def cmd_promote(settings: Settings, args: argparse.Namespace) -> int:
    pool = await _connect(settings)
    try:
        async with pool.acquire() as conn:
            promoted, inserted = await cand_db.promote_accepted(conn)
    finally:
        await pool.close()
    print(f"promote: {promoted} candidates promoted, {inserted} new phrase rows")
    return 0


async def _release_apply(settings: Settings, args: argparse.Namespace) -> int:
    """Idempotent seed job: load a release artifact into this environment.
    Releases are not migrations (deployment plan) — content that changes
    monthly doesn't belong in the checksummed migration chain."""
    import csv as _csv
    import hashlib

    release_dir = SEEDS_ROOT / "releases" / args.version
    manifest_bytes = (release_dir / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    csv_text = (release_dir / "phrases.csv").read_text(encoding="utf-8")

    csv_sha = hashlib.sha256(csv_text.encode("utf-8")).hexdigest()
    pinned = manifest["files"]["phrases.csv"]["sha256"]
    if csv_sha != pinned:
        print(f"apply: phrases.csv sha mismatch ({csv_sha} != {pinned}) — artifact corrupt", file=sys.stderr)
        return 2
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()

    rows = list(_csv.DictReader(io.StringIO(csv_text)))
    pool = await _connect(settings)
    try:
        async with pool.acquire() as conn:
            inserted, created = await cand_db.apply_release_rows(
                conn,
                version=args.version,
                manifest_sha256=manifest_sha,
                rows=[dict(r) for r in rows],
                notes=str(manifest.get("notes", "")),
            )
    finally:
        await pool.close()
    print(
        f"release apply {args.version}: {inserted} rows inserted "
        f"({len(rows) - inserted} already present), release row "
        f"{'created' if created else 'already registered'} — bump the trie cache version"
    )
    return 0


async def _release_retire(settings: Settings, args: argparse.Namespace) -> int:
    pool = await _connect(settings)
    try:
        async with pool.acquire() as conn:
            retired = await cand_db.retire_release(conn, version=args.version)
    finally:
        await pool.close()
    await _audit(
        settings,
        audit_kinds.RELEASE_PUBLISHED,
        {"version": args.version, "action": "retired", "rows_retired": retired},
    )
    print(
        f"release retire {args.version}: {retired} rows retired — bump the trie "
        "cache version (redis: autocomplete:tenant_phrase_version:*)"
    )
    return 0


async def cmd_release(settings: Settings, args: argparse.Namespace) -> int:
    if args.apply:
        return await _release_apply(settings, args)
    if args.retire:
        return await _release_retire(settings, args)
    fluency = build_fluency_filter(settings.kenlm_model_path)
    pool = await _connect(settings)
    try:
        async with pool.acquire() as conn:
            records = await cand_db.fetch_release_rows(conn)
            rows = [
                ReleaseRow(
                    phrase=str(r["phrase"]),
                    language=str(r["language"]),
                    specialty=r["specialty"],
                    section_hint=r["section_hint"],
                    source_kind=str(r["source_kind"]),
                    source_ref=r["source_ref"],
                    tier=r["tier"],
                    review_engine=r["review_engine"],
                    risk_flags=tuple(r["risk_flags"] or ()),
                )
                for r in records
            ]
            csv_text, manifest_json, manifest_sha = build_manifest(
                version=args.version, rows=rows, fluency_filter=fluency.name, notes=args.notes
            )
            out_dir = SEEDS_ROOT / "releases" / args.version
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "phrases.csv").write_text(csv_text, encoding="utf-8")
            (out_dir / "manifest.json").write_text(manifest_json, encoding="utf-8")
            await cand_db.stamp_release(
                conn,
                version=args.version,
                manifest_sha256=manifest_sha,
                phrase_count=len(rows),
                published_by=UUID(args.published_by) if args.published_by else None,
                notes=args.notes,
            )
    finally:
        await pool.close()
    await _audit(
        settings,
        audit_kinds.RELEASE_PUBLISHED,
        {"version": args.version, "phrase_count": len(rows), "manifest_sha256": manifest_sha},
    )
    print(f"release {args.version}: {len(rows)} phrases, manifest sha256={manifest_sha}")
    print(f"artifact: {out_dir}")
    return 0


async def cmd_prompts(settings: Settings, args: argparse.Namespace) -> int:
    """Derive a candidate ASR prompt for one (language, specialty) cell from
    the accepted corpus; with --promote, gate on two WER report JSONs."""
    if args.promote:
        incumbent = json.loads(Path(args.incumbent_report).read_text(encoding="utf-8"))
        candidate = json.loads(Path(args.candidate_report).read_text(encoding="utf-8"))
        inc_wer = float(incumbent["overall"]["wer"])
        cand_wer = float(candidate["overall"]["wer"])
        if prompt_dom.promote_gate(incumbent_wer=inc_wer, candidate_wer=cand_wer):
            print(f"PROMOTE: delta_pp={ (inc_wer - cand_wer) * 100:.2f} > 0")
            return 0
        print(f"REJECT: delta_pp={(inc_wer - cand_wer) * 100:.2f} ≤ 0 — incumbent stays")
        return 1

    pool = await _connect(settings)
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT phrase, language, COALESCE(specialty, 'general') AS specialty,
                       COALESCE(section_hint, '') AS section
                FROM autocomplete_phrases
                WHERE source = 'system' AND review_state = 'accepted' AND enabled = TRUE
                """
            )
    finally:
        await pool.close()
    cells: dict[prompt_dom.PromptCell, list[str]] = {}
    for r in rows:
        cell = prompt_dom.PromptCell(str(r["language"]), str(r["specialty"]), None)
        cells.setdefault(cell, []).append(str(r["phrase"]))
    target = prompt_dom.PromptCell(args.language, args.specialty, None)
    terms = prompt_dom.tf_idf_terms(cells, target=target)
    if not terms:
        print(f"prompts: no accepted corpus for {args.language}/{args.specialty}", file=sys.stderr)
        return 1
    prompt = prompt_dom.build_prompt(terms)
    print(prompt)
    print(
        f"# {prompt_dom.count_tokens(prompt)} tokens (limit {prompt_dom.ASR_PROMPT_MAX_TOKENS}); "
        "A/B with scripts/eval/run_per_section_wer.py, then `corpus-forge prompts --promote`",
        file=sys.stderr,
    )
    return 0


async def cmd_validate(settings: Settings, args: argparse.Namespace) -> int:
    """Validator v2 over a release artifact: seed-file checks per row +
    language-ID, near-dup, risk/tier, provenance, quota matrix, and (when
    MDX_LANGUAGETOOL_URL is set) morphology."""
    release_dir = Path(args.release_dir)
    csv_text = (release_dir / "phrases.csv").read_text(encoding="utf-8")
    errors = validate_release_csv(csv_text)

    reader = csv.DictReader(io.StringIO(csv_text))
    rows = [
        (r["phrase"], r["language"], r["specialty"] or None, r["section_hint"] or None)
        for r in reader
    ]
    quota_cfg = load_quota(SEEDS_ROOT / "quota.yaml")
    violations = check_quota(quota_cfg, rows)

    morphology_issues = 0
    if settings.languagetool_url:
        allowlist_path = SEEDS_ROOT / "risk" / "medical_allowlist.txt"
        allowlist = load_wordlist(allowlist_path) if allowlist_path.exists() else frozenset()
        for language in sorted({r[1] for r in rows}):
            issues = await languagetool.check_phrases(
                base_url=settings.languagetool_url,
                phrases=[r[0] for r in rows if r[1] == language],
                language=language,
                allowlist=allowlist,
            )
            for issue in issues:
                print(f"MORPHOLOGY {language}: {issue.phrase!r} — {issue.message} ({issue.token})")
            morphology_issues += len(issues)
    else:
        print("validate: MDX_LANGUAGETOOL_URL unset — morphology stage SKIPPED", file=sys.stderr)

    for e in errors:
        print(f"ERROR line {e.line} [{e.check}] {e.phrase!r}: {e.detail}")
    for v in violations:
        cell = v.cell
        print(
            f"QUOTA {v.bound}: {cell.language}/{cell.specialty}/{cell.section}/{cell.bucket} "
            f"actual={v.actual} target={cell.target} "
            f"[{cell.target * quota_cfg.lower:.0f}, {cell.target * quota_cfg.upper:.0f}]"
        )
    total = len(errors) + len(violations) + morphology_issues
    quota_note = "" if not args.skip_quota else " (quota advisory only)"
    if args.skip_quota:
        total = len(errors) + morphology_issues
    print(f"validate: {len(rows)} rows, {len(errors)} row errors, "
          f"{len(violations)} quota violations, {morphology_issues} morphology issues{quota_note}")
    return 1 if total else 0


# ── entrypoint ───────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="corpus-forge", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("mine", help="mine n-grams from finalized report versions")
    p.add_argument("--language", default="uk", choices=("uk", "en"))
    p.add_argument("--limit", type=int, default=20_000)

    p = sub.add_parser("gaps", help="telemetry prefixes with zero accepts (work queue)")
    p.add_argument("--limit", type=int, default=5_000)

    p = sub.add_parser("import", help="terminology importer + phrase-ification")
    p.add_argument("--dataset", required=True, choices=("drlz", "formulary", "nk025"))
    p.add_argument("--dataset-version", required=True, help="e.g. 2026-08-09")
    p.add_argument("--file", required=True)
    p.add_argument("--language", default="uk", choices=("uk", "en"))
    p.add_argument("--section", default="diagnosis", help="for line-based datasets")
    p.add_argument("--specialty", default=None)

    p = sub.add_parser("generate", help="boxed LLM generation into staging")
    p.add_argument("--language", required=True, choices=("uk", "en"))
    p.add_argument("--specialty", required=True)
    p.add_argument("--section", required=True)
    p.add_argument("--count", type=int, default=50)
    p.add_argument("--seed-term", action="append")

    p = sub.add_parser("jury", help="LLM jury over candidates of one tier")
    p.add_argument("--tier", type=int, required=True, choices=(1, 2))
    p.add_argument("--limit", type=int, default=500)
    p.add_argument("--external", action="store_true", help="external API (public-data candidates only)")
    p.add_argument("--calibrated", action="store_true", help="calibration gate passed (tier 1)")

    p = sub.add_parser("review", help="record one human decision")
    p.add_argument("--candidate-id", required=True)
    p.add_argument("--reviewer", required=True, help="reviewer users.sub UUID")
    p.add_argument("--decision", required=True, choices=("accept", "reject", "edit"))
    p.add_argument("--edited-text", default=None)
    p.add_argument("--latency-ms", type=int, default=None)

    sub.add_parser("promote", help="accepted candidates → live corpus (idempotent)")

    p = sub.add_parser("release", help="publish / apply / retire an immutable corpus release")
    p.add_argument("--version", required=True, help="vMAJOR.MINOR.PATCH")
    p.add_argument("--notes", default="")
    p.add_argument("--published-by", default=None)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="load the artifact into this environment (idempotent seed job)")
    mode.add_argument("--retire", action="store_true", help="rollback: retire the release's rows (register stays immutable)")

    p = sub.add_parser("prompts", help="derive ASR prompt / gate a promotion")
    p.add_argument("--language", default="uk")
    p.add_argument("--specialty", default="general")
    p.add_argument("--promote", action="store_true")
    p.add_argument("--incumbent-report", default=None)
    p.add_argument("--candidate-report", default=None)

    p = sub.add_parser("validate", help="validator v2 over a release artifact")
    p.add_argument("--release-dir", required=True)
    p.add_argument(
        "--skip-quota",
        action="store_true",
        help="report quota without failing (pre-v1.0.0 partial corpora)",
    )
    return parser


_COMMANDS = {
    "mine": cmd_mine,
    "gaps": cmd_gaps,
    "import": cmd_import,
    "generate": cmd_generate,
    "jury": cmd_jury,
    "review": cmd_review,
    "promote": cmd_promote,
    "release": cmd_release,
    "prompts": cmd_prompts,
    "validate": cmd_validate,
}


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    settings = load_settings()
    return asyncio.run(_COMMANDS[args.command](settings, args))


if __name__ == "__main__":
    sys.exit(main())
