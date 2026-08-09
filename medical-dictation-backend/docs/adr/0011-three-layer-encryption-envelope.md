# ADR-0011 — 3-layer encryption envelope (KEK_master → KEK_tenant → DEK_object)

**Date:** 2026-05-22
**Status:** Accepted
**Deciders:** security lead, tech lead, DPO

---

## Context

Sprint 03 stores the first PHI bytes (audio recordings) at rest. GDPR
and Law 2297-VI place hard constraints: data at rest must be encrypted,
keys must be rotatable, and a single compromised credential must not
expose all tenants' data.

Options for the envelope:

| Approach                         | Blast radius on rotation     | Key proliferation | KMS calls/op |
| -------------------------------- | ---------------------------- | ----------------- | ------------ |
| Single master key, no per-object DEK | All tenants impacted on rotate | 1                 | 2 per op     |
| KMS direct per-object (KMS GenerateDataKey) | None / smallest         | N per object       | 2 per op (high cost) |
| **3-layer envelope** (this ADR)  | Per-tenant on master rotate; per-object DEK changes are free | Bounded            | 0 (KEK_tenant cached) |

## Decision

Adopt a 3-layer envelope:

1. **KEK_master** — one per environment, file-based in dev, KMS-backed
   in prod (sprint 16). Wraps every tenant KEK.
2. **KEK_tenant** — one per tenant, 32 bytes, stored wrapped in the
   `tenant_keks` table. Plaintext is cached in-process for ≤ 60 s.
3. **DEK_object** — fresh 32 bytes for every object. Wrapped by
   `KEK_tenant`. Stored alongside the ciphertext in the object header.

All three layers use AES-256-GCM. AAD is `tenant_id.bytes || caller_aad`
on every operation, so cross-tenant blob mixups fail at GCM tag check.

## Consequences

- **Rotation cost is bounded**: rotating `KEK_master` re-wraps N tenant
  KEKs (one row each) — bounded by tenant count, not object count.
  Rotating a `KEK_tenant` re-wraps M DEKs (per-object, but lazy: only
  needed if the tenant KEK is destroyed; otherwise old DEKs decrypt
  fine until the next read forces a re-wrap).
- **KMS migration (sprint 16) is a 1-line swap** of
  `FileMasterKeyProvider` → `KmsMasterKeyProvider` plus a one-time
  re-wrap of every `tenant_keks.wrapped_kek` under the KMS-backed
  master.
- **Per-object DEK** means identical plaintexts produce different
  ciphertexts. Defends against rainbow-style analysis.
- **No KMS call per object**: tenant KEK plaintext is cached. KMS load
  is one call per tenant per cache TTL — well within free-tier quotas.

## Re-wrap procedure (sprint 16)

For each row in `tenant_keks`:

```
plaintext_kek = FileMasterKeyProvider.unwrap(row.wrapped_kek)
master_key_id, new_wrapped = KmsMasterKeyProvider.wrap(plaintext_kek)
UPDATE tenant_keks SET wrapped_kek = new_wrapped, kek_master_id = master_key_id;
```

Bounded by tenant count. No need to re-wrap DEKs — they live under the
tenant KEK which doesn't change.

## Alternatives considered

- **Single master, no per-object DEK**: rejected — a credential leak
  exposes every object until the master is rotated, which takes hours.
- **KMS-direct per-object**: rejected — at 100k objects/day per tenant,
  KMS API quotas are exceeded; the cost is ~$50/tenant/day at AWS list
  pricing. Tenant KEK caching solves this.

## Trigger conditions for revisiting

- ~~Sprint 16 KMS migration completes and the re-wrap script runs cleanly.~~ Done — see amendment.
- A new regulator requires per-object KMS attestation (sprint 18+).

## Amendment (sprint 16) — the promised swap, as built

`KmsMasterKeyProvider` is implemented against **Vault Transit**
(self-hostable in-jurisdiction, ADR-0006 residency posture); the master
KEK never leaves Vault, `master_key_id = vault:{mount}:{key}`, and the
≤60 s tenant-KEK cache keeps Transit load at one call per tenant per
TTL, exactly as designed above. Two refinements over the original
sketch:

- The "1-line swap" is `crypto.build_master_key_provider(...)` —
  settings-gated (`MDX_MASTER_KEY_PROVIDER=file|vault` + `MDX_VAULT_*`),
  used at every composition site. Dev default stays `file`.
- Because the re-wrap runs row-by-row, the migration window has mixed
  masters; `CompositeMasterKeyProvider` routes `unwrap` by the stored
  `kek_master_id` (vault primary, file fallback **iff the key file is
  still present**), so every old row keeps decrypting mid-migration and
  removing the file finishes the cutover.

The re-wrap procedure above is `scripts/kms/rewrap-tenant-keks.py`
(transactional per row, resumable, `--dry-run`, verifies the new
wrapping round-trips before commit, audits `kms.rewrap.completed` sec).
Fail-closed startup: vault selected + unreachable → the service refuses
to start (`docs/runbooks/kms.md`). The signing-service system HMAC keys
ride the same trust root via Vault KV (`MDX_HMAC_KEYS_FROM_VAULT`).
