# EVA-S01 — Sign-off register

Each row names the artifact **and the verification that backs it** (rule §20).
Countersign by replacing ⏳ with name + date.

## Tech lead

| Artifact | Verification | Status |
|---|---|---|
| `libs/evidence_models` contracts v1 | 11 unit tests; `make ci` green (lint, mypy --strict, leaf gate, contracts gate, security) | ⏳ |
| Additive-only policy in CI | 12 linter unit tests both directions; `contracts-check` drift negative proof | ⏳ |
| Migration 0067 (11 tables) | up→down→up live cycle; `check-rls` 48 tables PASS | ⏳ |
| dictat typegen | `npm run verify:evidence-types` (strict tsc) green | ⏳ |

## Security

| Artifact | Verification | Status |
|---|---|---|
| RLS + FORCE + RESTRICTIVE on all 11 tables | crafted probes: cross-tenant 0 rows, unscoped 0 rows, injected tenant_id rejected | ⏳ |
| Dual-key privacy on questions/answers | same-tenant other-user reads 0 rows | ⏳ |
| Append-only `answer_provenance` | UPDATE/DELETE denied at grant AND trigger layer (superuser probe) | ⏳ |
| Permission matrix + `knowledge_admin` | bidirectional drift test 18/18; explicit false on all 43 pre-existing pairs; negative proof run | ⏳ |

## Clinical advisor (AC-S01-B-6)

| Artifact | Verification | Status |
|---|---|---|
| Segment taxonomy + strength scale + fact source/status enums | ADR-0002 decision table; enums frozen in contracts + DB CHECKs | ⏳ **required before tagging v1.0.0** |

## SRE

| Artifact | Verification | Status |
|---|---|---|
| Migration reversibility + seed | 0067 down documented caveat (knowledge_admin users); `make seed` idempotent re-run green | ⏳ |
