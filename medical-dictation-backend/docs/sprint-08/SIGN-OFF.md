# Sprint 08 — Sign-Off

**Sprint dates:** 2026-05-13 → 2026-05-26
**Status:** ✅ Code complete; load test + sign-offs pending pilot env.

## Scope delivered

- ✅ **Day 1 (versioning data model)**: migrations 0016/0017/0018,
  `libs/report_models` with strict Pydantic + canonical-JSON
  serialiser, deferrable FK two-step insert pattern.
- ✅ **Day 2 (status lifecycle)**: `ReportStateMachine` with all
  transitions from spec §2.2 (incl. concurrent detection, 1h
  revert window, primary-author check, finalize validation).
- ✅ **Day 3 (optimistic lock + autosave)**: PUT
  `/v1/reports/{id}/draft` with `expected_version`, body-hash
  idempotency, AutosaveRateLimiter, idle-draft cleanup query.
- ✅ **Day 4 (amendments + chain integrity)**: POST
  `/v1/reports/{id}/amend`, chain reconciler shape, Hypothesis
  property test (200 examples × ~3s).
- ✅ **Day 5 (diff)**: GET `/v1/reports/{id}/diff` with section + metadata
  diff, char-level segments via difflib, LRU DiffCache.
- ✅ **Day 6 (search + cursor + redaction + read-purpose)**: GET
  `/v1/reports/search` with `simple` FTS, GIN indexes, cursor
  pagination, `ts_headline` snippet, PII redactor.
- ✅ **Day 7 (audit + observability)**: 10 audit kinds (including
  aggregated `report.draft.updated`), Grafana dashboard, Prometheus
  alert rules.
- ✅ **Day 8 (chain reconciler + draft cleanup)**: cron 04:30 UTC
  job in `jobs/chain_reconciler.py`, read-only investigation tool at
  `scripts/admin/report_chain_repair.py`, idle-draft service method
  ready for sprint-16 scheduler.
- ✅ **Day 9 (load test scaffolding)**: 100k-seed script, k6 scenarios,
  `docs/eval/sprint-08-loadtest.md` with EXPLAIN ANALYZE template +
  thresholds + partition trigger.
- ✅ **Day 10 (docs)**: ADR-0020 append-only versioning, ADR-0021
  `simple` FTS, `docs/architecture/reports.md`,
  `docs/runbooks/reports.md`, audit-kinds-sprint-08.

## Tests

- `libs/report_models`: **9/9 passing**.
- `report-service` unit + property: **54/54 passing** (8 unit modules
  + 6 chain-integrity property cases).
- Cumulative sprint 03–08 unit tests: **244 passing**.

## Out of scope (deliberate)

- Real-time collaborative editing.
- Custom field types beyond sprint-06 templates.
- OpenSearch / Elasticsearch.
- Ukrainian FTS dictionary.
- ML-based diff summarization.
- Audio replay from sentence (sprint-15).
- FHIR export (sprint-17).
- Report PDF generation (sprint-09 owns for signing).

## Sign-offs

| role                | name      | date      |
| ------------------- | --------- | --------- |
| Tech lead           | _pending_ | _pending_ |
| Security lead       | _pending_ | _pending_ |
| DPO                 | _pending_ | _pending_ |
| Clinical lead       | _pending_ | _pending_ |
| Frontend lead       | _pending_ | _pending_ |
| SRE/DevOps          | _pending_ | _pending_ |

## Known follow-ups

1. Establish actual load-test baseline against a real pilot DB.
2. Wire idle-draft cleanup scheduler (sprint-16).
3. CI gate `check-report-create-helper-only` to enforce the two-step
   insert helper (lint mentioned in ADR-0020; not yet wired).
4. Sprint-09 picks up canonical_content_bytes and the empty
   signing fields.
