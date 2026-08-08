# EVA-S02 — Retro (2026-08-05)

**R1 — verification catches what unit tests can't.** The live E2E surfaced
four real integration defects no unit test saw: the tenant_keks FK (global
tenant needed a tenants row), the missing chunks UPDATE grant, asyncpg's
date-param strictness, and — the important one — the no-op dedup path
declaring `done` over a half-embedded version. The Dictate discipline of
"prove it live before sign-off" earned its keep in one afternoon.

**R2 — assumptions about bundled software.** ADR-0003 assumed analysis-icu
ships with OpenSearch (it doesn't) and the compose file assumed clamav
publishes arm64 under plain version tags (it doesn't). Both were cheap to
fix in dev but would have been image-build failures on the rig. → Rule of
thumb recorded in CLAUDE.md gotchas; on-prem image bake list started.

**R3 — the S00 debt keeps taxing.** The gateway had to be built mid-sprint
(planned for S00), and the batch-A harness/Grafana board still don't exist,
so S02's observability section is metrics-in-code without dashboards. The
debt list is now explicit in SPRINT-TODO with S03 blockers marked.

**R4 — CPU embedding reality.** BGE-M3 on the Mac embeds at a rate that
makes a real 100k-chunk load a multi-day affair — the deferral of
AC-S02-B-1 to the rig was the right product call, and the loadtest script
cleanly separates "pipeline correctness" (proven) from "bulk capacity"
(measured synthetically, re-measured on the rig).

**What went well.** Parallelizing the parsers to a subagent against a frozen
`ParsedDocument` contract produced 16 passing golden tests with zero
interface friction; the platform recon again prevented API misuse
(messaging `headers["_id"]`, S3Client's missing create_bucket, ServiceState
pattern).

**Metric.** 2 services, 1 lib, 1 migration, 5 parsers, 69 unit tests
(41+16+8+4), 5 audit kinds, live E2E incl. quarantine/retraction/snapshot
lifecycles — one working day.
