#!/usr/bin/env python3
"""One-time KMS migration — re-wrap every tenant KEK under Vault Transit.

ADR-0011's promised re-wrap procedure (sprint 16). Moves each
``tenant_keks`` row from the file master (``file-v1``) to the Vault
Transit master (``vault:{mount}:{key}``). The envelope hierarchy below
the tenant KEK is untouched: no DEK, no object, no blob changes — which
is exactly why existing encrypted objects keep decrypting afterwards.

Properties:

- **Transactional per row** — each KEK re-wraps in its own transaction
  under ``SELECT … FOR UPDATE``; a crash mid-migration leaves a mixed
  but fully consistent table.
- **Resumable** — rows already wrapped under the target Vault key are
  skipped; re-running converges.
- **Verified before commit** — the new wrapping is round-tripped through
  Vault and byte-compared against the plaintext before UPDATE.
- **Dry-run** — ``--dry-run`` unwraps and reports, writes nothing.
- **Audited** — one ``kms.rewrap.completed`` (sec) event per migrated
  tenant, written through libs/audit (rule 5).

Usage (see docs/runbooks/kms.md):

    MDX_VAULT_ADDR=http://localhost:8200 MDX_VAULT_TOKEN=… \\
    uv run --project libs/crypto python scripts/kms/rewrap-tenant-keks.py \\
        --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import asyncpg

DEFAULT_CRYPTO_DSN = (
    "postgresql://crypto_writer:crypto_writer@localhost:5432/medical_dictation"
)
DEFAULT_AUDIT_DSN = (
    "postgresql://audit_writer:audit_writer@localhost:5432/medical_dictation"
)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true", help="report; write nothing")
    p.add_argument(
        "--crypto-dsn",
        default=os.environ.get("DB_CRYPTO_WRITER_DSN", DEFAULT_CRYPTO_DSN),
        help="crypto_writer DSN (the only role that may write tenant_keks)",
    )
    p.add_argument(
        "--audit-dsn",
        default=os.environ.get("DB_AUDIT_WRITER_DSN", DEFAULT_AUDIT_DSN),
        help="audit_writer DSN for the kms.rewrap.completed events",
    )
    p.add_argument(
        "--master-key-path",
        default=os.environ.get("MDX_MASTER_KEY_PATH", "infra/dev/master.key"),
        help="path to the file master key (source of the re-wrap)",
    )
    p.add_argument("--vault-addr", default=os.environ.get("MDX_VAULT_ADDR", "http://localhost:8200"))
    p.add_argument("--vault-token", default=os.environ.get("MDX_VAULT_TOKEN", ""))
    p.add_argument("--vault-key", default=os.environ.get("MDX_VAULT_TRANSIT_KEY", "mdx-master"))
    p.add_argument("--vault-mount", default=os.environ.get("MDX_VAULT_TRANSIT_MOUNT", "transit"))
    return p.parse_args()


async def _run(args: argparse.Namespace) -> int:
    from audit import AuditWriter, Severity
    from crypto import (
        FileMasterKeyProvider,
        KmsMasterKeyProvider,
        MasterKeyError,
    )
    from crypto.master import MASTER_KEY_SIZE_BYTES

    file_provider = FileMasterKeyProvider(path=args.master_key_path)
    try:
        await file_provider.startup_self_check()
    except MasterKeyError as exc:
        print(f"FATAL: file master unusable: {exc}", file=sys.stderr)
        return 2

    vault = KmsMasterKeyProvider(
        addr=args.vault_addr,
        token=args.vault_token,
        key_name=args.vault_key,
        mount=args.vault_mount,
    )
    try:
        await vault.startup_self_check()
    except MasterKeyError as exc:
        print(f"FATAL: Vault Transit unusable: {exc}", file=sys.stderr)
        return 2
    target_id = vault.master_key_id

    pool = await asyncpg.create_pool(args.crypto_dsn, min_size=1, max_size=2)
    audit_pool = await asyncpg.create_pool(args.audit_dsn, min_size=1, max_size=2)
    audit_writer = AuditWriter(audit_pool)

    migrated = 0
    skipped = 0
    failed = 0
    try:
        rows = await pool.fetch(
            "SELECT tenant_id, kek_master_id FROM tenant_keks ORDER BY tenant_id"
        )
        print(f"tenant_keks: {len(rows)} row(s); target master {target_id}")
        for row in rows:
            tenant_id = row["tenant_id"]
            source_id = row["kek_master_id"]
            if source_id == target_id:
                skipped += 1
                print(f"  skip   {tenant_id} (already {target_id})")
                continue

            if args.dry_run:
                # Prove the source row unwraps — the strongest no-write check.
                r = await pool.fetchrow(
                    "SELECT wrapped_kek, kek_master_id FROM tenant_keks WHERE tenant_id = $1",
                    tenant_id,
                )
                assert r is not None
                provider = file_provider if file_provider.handles(r["kek_master_id"]) else vault
                plaintext = await provider.unwrap(r["kek_master_id"], bytes(r["wrapped_kek"]))
                ok = len(plaintext) == MASTER_KEY_SIZE_BYTES
                plaintext = b"\x00" * MASTER_KEY_SIZE_BYTES  # noqa: F841 — best-effort zero
                migrated += 1
                print(f"  would  {tenant_id} ({source_id} -> {target_id}) unwrap_ok={ok}")
                continue

            try:
                async with pool.acquire() as conn, conn.transaction():
                    locked = await conn.fetchrow(
                        "SELECT wrapped_kek, kek_master_id FROM tenant_keks "
                        "WHERE tenant_id = $1 FOR UPDATE",
                        tenant_id,
                    )
                    assert locked is not None
                    if locked["kek_master_id"] == target_id:
                        skipped += 1  # raced with a concurrent run — fine
                        continue
                    src = (
                        file_provider
                        if file_provider.handles(locked["kek_master_id"])
                        else vault
                    )
                    plaintext = await src.unwrap(
                        locked["kek_master_id"], bytes(locked["wrapped_kek"])
                    )
                    try:
                        new_id, new_wrapped = await vault.wrap(plaintext)
                        # Verify the new wrapping round-trips BEFORE commit.
                        back = await vault.unwrap(new_id, new_wrapped)
                        if back != plaintext:
                            raise MasterKeyError(
                                "post-wrap verification mismatch — refusing to commit"
                            )
                    finally:
                        plaintext = b"\x00" * MASTER_KEY_SIZE_BYTES
                        back = b"\x00" * MASTER_KEY_SIZE_BYTES  # noqa: F841
                    await conn.execute(
                        "UPDATE tenant_keks SET wrapped_kek = $2, kek_master_id = $3 "
                        "WHERE tenant_id = $1",
                        tenant_id,
                        new_wrapped,
                        new_id,
                    )
                migrated += 1
                print(f"  rewrap {tenant_id} ({source_id} -> {target_id})")
                await audit_writer.write_event(
                    tenant_id=tenant_id,
                    kind="kms.rewrap.completed",
                    actor_sub=None,
                    actor_role="kms-rewrap-script",
                    target_kind="tenant_kek",
                    target_id=str(tenant_id),
                    payload={"from": source_id, "to": new_id},
                    severity=Severity.SEC,
                )
            except Exception as exc:  # noqa: BLE001 — keep sweeping; row untouched
                failed += 1
                print(
                    f"  FAILED {tenant_id}: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
        verb = "would migrate" if args.dry_run else "migrated"
        print(f"done: {verb} {migrated}, skipped {skipped}, failed {failed}")
        return 1 if failed else 0
    finally:
        await vault.aclose()
        await pool.close()
        await audit_pool.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(_run(_parse_args())))
