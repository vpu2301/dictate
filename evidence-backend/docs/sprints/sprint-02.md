# EVA-S02 — Backend — Evidence Ingestion

**Objective:** Ship `evidence-ingest`: the full pipeline (parse → normalize →
chunk → enrich → dedup → embed → index → snapshot), retraction handling, the
ingest-time prompt-injection screen, and the operator CLI — resumable,
dead-lettered, and provenance-complete.

*(The framework planning file is
`~/Desktop/evidence-framework/docs/sprints/sprint-02-evidence-ingestion.md`;
this file records what was built and where it diverged.)*

---

## As-built (2026-08-05)

### What shipped, where

| Deliverable | Location | Proof |
|---|---|---|
| `evidence-model-gateway` v0 (:8015) — the S00 leftover, unblocked here | `services/evidence-model-gateway/` — role registry, `embed.dense` = BGE-M3 (CPU, sentence-transformers), /roles /healthz /readyz /v1/embed, optional service token | 8 unit tests; live: embeds uk text at 1024-dim; readyz asserts model+dimension |
| `libs/models` gateway client (rule E12) | `libs/models/` — retries/backoff, fail-closed `GatewayUnavailableError` | 4 unit tests (mock transport) |
| `check-no-direct-model` + `check-no-os-environ` gates | `scripts/dev/`, wired into `make ci` | negative proofs run (planted torch import / os.getenv → gate fails) |
| `evidence-ingest` (:8010) | `services/evidence-ingest/` — config/main/deps (platform template pattern), routers (jobs, quarantine decision, snapshots, retractions, stats), pipeline, worker (`python -m evidence_ingest.worker`), CLI (`evidence-ingest …`) | 41 unit tests + live E2E below |
| 5 parsers + golden fixtures | `domain/parsers/` (pdf, pmc_xml JATS+MEDLINE, html_guideline, markdown, docx; shared metadata/lang helpers; XXE-safe lxml) | 16 golden-file tests incl. corrupt-PDF and XXE probes |
| Structure-aware chunking | `domain/chunking.py` — headings are hard bounds, tables whole, 350–700-token targets, offsets into canonical full text | 6 unit tests |
| Injection screen v1 (LM4) | `domain/injection_screen.py` — 10 pattern families, en+uk | 12/12 probes quarantined, 0/6+4 false positives (unit) + live quarantine E2E |
| Dedup + versioning | `domain/enrich.py` (canonical id: DOI>PMID>PMCID>МОЗ>ISBN>sha256) + upsert/version logic in pipeline | live: unchanged → NOOP; changed → version 2 (`doi:10.3333/moz.2022.15` v2) |
| Migration `0068_evidence_ingest` | platform chain — ingest_jobs/ingest_errors/quarantine (RLS+FORCE), global-tenant read amendment on corpus tables, `chunks` UPDATE grant, reserved global tenant row | up→down→up cycle ×3; platform `check-rls` 51 tables PASS |
| OpenSearch lexical index | `adapters/search.py` — `medical_icu` analyzer (icu_tokenizer+icu_folding), retracted-flag projection | live uk/en BM25 queries return ranked hits |
| Compose | `infra/compose/evidence.yaml` — opensearch (installs analysis-icu on boot), clamav (arm64 tag), eva-corpus bucket init, service entries behind a profile; joins `medical-dictation_default` externally | containers healthy; clamd INSTREAM scan exercised in the live run |
| Snapshots + licensing | `domain/snapshots.py` + `constants.ALLOWED_SNAPSHOT_LICENSES` + `docs/corpus/licenses.md` | live: fixtures-v1 = 7 members, 1 license-excluded |
| Retractions | `domain/retractions.py` (CSV feed, DOI/PMID) | live: flagged 1; fixtures-v2 = 6 members; fixtures-v1 unchanged (immutability) |
| Audit kinds ×5 | `audit_kinds.py` + platform catalogue EVA-S02 section | live chain: 9 ingested, 1 quarantined, 1 decided, 1 retracted, 2 snapshots |
| ADR-0003 index tech (spec "ADR-000D") | `docs/adr/0003…` | committed |
| Corpus sourcing plan (user request) | `~/Desktop/evidence-corpus-integration-plan.md` — 12 free sources w/ licenses, P0–P2 phases | delivered |
| Runbook | `docs/runbooks/evidence-ingest.md` | committed |

### Live E2E transcript (fixture corpus, virus scan ON via clamd)

6 docs DONE end-to-end (parse→clamd→chunk→enrich→embed(BGE-M3)→OpenSearch),
`corrupt.pdf` → DEAD with `pdf_read_failed` while the run continued; a
malicious markdown → QUARANTINED (2 patterns), operator CLI `quarantine
decide rejected` → job dead (audited); re-run → all NOOP (idempotence);
changed doc → version 2; interrupted job → RESUME from its recorded stage.

### As-built deltas

1. **Gateway v0 scope**: only `embed.dense` is served (S02 needs nothing
   else). Generator roles are registry entries to be added on the rig; the
   candidate set (Gemma 3 / Kimi K2) is recorded in `docs/models/PINS.md`
   per the product owner's direction.
2. **AC-S02-B-1 (≥100k real chunks) is deferred to the operator bulk run**
   (product-owner decision): the pipeline is proven on the fixture corpus,
   the index path is exercised at 100k scale synthetically
   (`scripts/loadtest/volume_100k.py`, results below), and the sourcing plan
   for the real load is the Desktop integration plan. CPU embed throughput
   makes a Mac-local 100k real-embedding run infeasible (~days); the 6 h
   backlog NFR is a rig target.
3. **analysis-icu is NOT bundled** with OpenSearch 2.19 (ADR-0003 assumed it
   was) — the compose entry installs it on first boot; the on-prem image
   must bake it.
4. **`chunks` UPDATE grant** (0067 gap): the embedding backfill needs UPDATE;
   granted in 0068 with a strict tenant policy.
5. **Reserved global tenant row**: `tenant_keks.tenant_id` FK requires a
   tenants row, so 0068 inserts the nil-uuid tenant (`suspended`,
   `is_active=false`) — invisible to operator surfaces (no memberships).
6. **Global-corpus quarantine review is an operator CLI command**
   (`quarantine list|decide`) — the HTTP endpoint authorizes tenant
   knowledge_admins whose RLS scope can never see nil-uuid rows.
7. **License-unknown parking**: no enum change; unknown/missing license ⇒
   `restricted` + `license_known=false`, and `restricted` is excluded from
   snapshots (constants). The S01 enum stays frozen.
8. **Quarantined jobs block snapshots** (state ∉ done/dead) — deliberate
   fail-safe: you don't freeze a corpus with an undecided suspicious doc.
9. **Pipeline defect found & fixed during verification**: the
   unchanged-checksum no-op path could declare `done` over a version left
   unembedded by an earlier crash; it now resumes embedding when pending
   chunks exist (regression captured in the live repair run).
10. **pip-audit is informational** (platform posture, `|| true`): the only
    current finding is `ecdsa` via the platform's python-jose, no fixed
    release available.
11. **Spec §2's optional pg-lexical GIN fallback not created** — OpenSearch
    is the lexical engine (ADR-0003); adding both would double write paths.
12. Harness checks A-8…A-11 and the Grafana board are deferred with the rest
    of the S00 harness work (SPRINT-TODO).

### Measured numbers (Mac dev host, CPU)

- Parse+chunk+enrich per fixture doc: 0.03–0.5 s (≫ 50 docs/min NFR).
- `embed.dense` CPU: 5.7 chunks/s (100k ≈ 4.9 h — inside the 6 h NFR);
  pgvector+HNSW: 100k rows in 1557 s (64 rows/s at full graph); OpenSearch
  bulk: ~9,200 docs/s; ANN top-10 @100k: 21 ms; BM25 top-10 @100k: 39 ms.
  Full table + lessons: `docs/sprints/sprint-02-loadtest.md`.

### Acceptance criteria

- AC-S02-B-1 ⏳ deferred to the rig bulk run (delta 2) — pipeline + index
  path proven; sourcing plan delivered.
- AC-S02-B-2 ✅ versioned re-ingest + dedup proven live.
- AC-S02-B-3 ✅ retraction lifecycle incl. snapshot semantics proven live.
- AC-S02-B-4 ✅ dead-letter + resumability proven live (corrupt fixture;
  RESUME path; stream retry/DLQ machinery from libs/messaging in the worker).
- AC-S02-B-5 ✅ injection screen 100% probe quarantine, false-positive check,
  reviewer flow audited (operator CLI variant — delta 6).
- AC-S02-B-6 ✅ license enforcement proven (1 excluded); register started.
- AC-S02-B-7 ✅ (dev-host half) — all write/query paths measured at 100k scale (sprint-02-loadtest.md); rig-profile latencies pending.
