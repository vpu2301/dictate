# Sprint-08 — Implementation Plan (as-built)

## Day 1 — Versioning data model

- [x] `infra/postgres/migrations/0016_create_reports.sql` (head row +
  RLS + status enum + denormalised search-path columns)
- [x] `infra/postgres/migrations/0017_create_report_versions.sql`
  (append-only versions + deferrable FK + subquery RLS)
- [x] `infra/postgres/migrations/0018_create_report_chain_failures.sql`
  (audit-schema reconciler scratch)
- [x] `libs/report_models` — strict Pydantic models, canonical bytes,
  diff DTOs, read-purpose enum (9 unit tests)

## Day 2 — Status lifecycle

- [x] `domain/report_lifecycle.py` — state machine
- [x] `domain/finalize_validator.py` — required sections + ICD-10
- [x] 11 state machine unit tests (concurrent + cross-user covered)

## Day 3 — Optimistic locking + autosave

- [x] `domain/reports_repository.py` — repo with body_hash idempotency
- [x] `domain/conflicts.py` — exceptions
- [x] `domain/autosave_rate_limit.py` — per-draft rate limiter
- [x] `routers/reports_drafts.py` — PUT /v1/reports/{id}/draft
- [x] `domain/draft_audit_buffer.py` — aggregated audit (per session)
- [x] `jobs/idle_draft_cleanup.py` — SQL ready for sprint-16 scheduler

## Day 4 — Amendments + chain property test

- [x] `routers/reports_amend.py` — POST /amend + POST /sign (501)
- [x] `domain/chain_integrity.py` — pure-Python verifier
- [x] `tests/property/test_amendment_chain.py` — Hypothesis 200 examples

## Day 5 — Diff

- [x] `domain/diff_engine.py` — section + metadata diff
- [x] `domain/diff_cache.py` — LRU
- [x] `routers/reports_diff.py` — GET /diff with version_id or _number

## Day 6 — Search + cursor + redaction

- [x] `domain/search.py` — query builder + cursor + snippet
- [x] `domain/pii_redactor.py` — IPN + PIB + DOB-like
- [x] `routers/reports_search.py` — GET /search + read-purpose

## Day 7 — Audit + observability

- [x] `audit_kinds.py` — 10 new sprint-08 kinds
- [x] `main_deps.py` — DraftAuditBuffer flush + metric handles
- [x] `monitoring/grafana/sprint-08-reports.json`
- [x] `monitoring/prometheus/sprint-08-alerts.yml`

## Day 8 — Chain reconciler + admin tool

- [x] `jobs/chain_reconciler.py` — runs the verifier per-tenant
- [x] `scripts/admin/report_chain_repair.py` — read-only dump

## Day 9 — Load test scaffolding

- [x] `scripts/loadtest/sprint-08-seed.py` — 100k seed
- [x] `scripts/loadtest/sprint-08-k6.js` — autosave/search/diff
  scenarios + thresholds
- [x] `docs/eval/sprint-08-loadtest.md`

## Day 10 — ADRs + docs + sign-off

- [x] ADR-0020 append-only versioning
- [x] ADR-0021 Postgres `simple` FTS
- [x] `docs/architecture/reports.md`
- [x] `docs/runbooks/reports.md`
- [x] `docs/audit/audit-kinds-sprint-08.md`
- [x] SIGN-OFF, RETRO, SPRINT-TODO
- [x] memory entry + index
