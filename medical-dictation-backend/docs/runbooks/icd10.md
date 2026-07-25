# Runbook — ICD-10 (МКХ-10) reference table

Sprint 13. Table: `icd10_codes` (migration `0054`). Loader:
`scripts/load-icd10.py`. Consumers: `GET /v1/icd10/search`
(report-service, the FE diagnosis picker) and the nlp extractor
(`nlp_service/stages/icd10_repository.py`). Both rank through the one
shared statement `db.ICD10_SEARCH_SQL`.

## Data acquisition — the honest position (2026-07-22)

**What shipped:** the full machinery (table, loader, search, ranking)
plus a **committed fixture of 239 hand-checked codes**
(`infra/seeds/icd10/fixture.csv`) spanning 18 chapters, including the
families the pilot specialties actually dictate — I10–I15
(hypertension), I20/I21/I25 (IHD), E10/E11 (diabetes), J44/J45
(COPD/asthma), plus common presentations (R05 кашель, R51 головний
біль, M54 дорсалгія) and the allergy codes (T78.2/T78.4) the
sprint-13 anamnesis template needs.

**What did NOT ship:** the complete МКХ-10-АМ table (~14 000 codes).
Ukraine mandates **МКХ-10-АМ** — the Australian modification (НК
025:2021), not plain WHO ICD-10. Timeboxed investigation found no
official МОЗ/НСЗУ endpoint publishing it as a downloadable CSV/XML
under clear redistribution terms: the classifier is distributed
through eHealth central-database dictionaries and commercial
publications, and the AM base is licensed material. **We did not
fabricate a partial table and call it complete** — the fixture is
labelled as a fixture everywhere it appears.

The fixture's codes and titles are published WHO/МКХ-10 classification
facts, which is why committing them is safe. The acquisition of the
full authoritative table is a named item in `todo.md` (owner: clinical
lead + ops).

**Consequence for users until then:** the picker and the extractor
only ever surface codes that exist in the loaded table. A missing code
means the clinician types the diagnosis as prose (`structured_diagnosis`
keeps its free text) — nothing is mis-coded, only un-coded.

## Loading the fixture (dev / CI)

```bash
make dev-up && make migrate-up
make seed-icd10-fixture
```

Idempotent — a second run reports `0 inserted, 0 updated, N unchanged`.

## Loading the full table (ops, once acquired)

```bash
# 1. Validate without writing. Fix every reported line before proceeding.
uv run python scripts/load-icd10.py --file /path/to/mkx10-am.csv --dry-run

# 2. Load (single transaction; parents are ordered before children).
uv run python scripts/load-icd10.py --file /path/to/mkx10-am.csv \
    --dsn postgresql://tenant_writer:...@<host>:5432/medical_dictation

# 3. Confirm idempotence — re-run step 2; expect 0 inserted / 0 updated.
```

Expected CSV columns: `code,display_uk,display_en,parent_code,chapter,is_leaf`
(only `code` + `display_uk` are required per row). If the real МОЗ
export uses a different dialect — a code shape the regex rejects, or a
different column set — **adjust `CODE_RE` in the loader, the `CHECK`
constraint in migration 0054, and `Icd10Code` in `libs/report_models`
together**, and extend the fixture with a real example of the new
shape. The fixture is the dialect contract; it is what the tests
assert against.

## Reload policy

Reloading changes what the extractor can propose, so treat it like a
pipeline change, not routine data maintenance:

- Reload in a **maintenance window**, not mid-clinic.
- Reloads are **additive-safe**: existing report content stores codes
  as strings (`section.icd10`), so already-signed reports are
  unaffected — signatures cover content bytes, not the reference table.
- A code that disappears from a new edition stays valid in old reports
  and simply stops being proposable. Do **not** delete rows to "clean
  up"; load the new edition over the old one.
- Announce reloads to the clinical lead: the extractor's proposal set
  changes on the same day.
- nlp replay fixtures pin the committed fixture table, so a prod
  reload never invalidates the pipeline's replay determinism tests.

## Verification after a load

```bash
# counts + a spot check of the ranking ladder
RUN_DB_INTEGRATION=1 uv run --project services/report-service \
  pytest services/report-service/tests/integration/test_icd10_search.py -v -s
```

The latency test prints `p50/p95/max` and the table size; the budget
is **p95 ≤ 50 ms**. Measured on the fixture (239 codes) p95 ≈ 0.9 ms;
on a 12 189-code synthetic expansion (full-table scale) p95 ≈ 1.7 ms.
If a real full table ever misses the budget, the first lever is a
`pg_trgm` GIN index for the prefix tier — not needed at this size.

## Why no RLS

`icd10_codes` is a global published classification with no tenant or
patient dimension, like `medical_prompts` (0008) and `voice_commands`
(0011). It is allowlisted in `scripts/ci/check-rls-policies.py` with a
pointer here. Services hold `SELECT` only; the loader runs as
`tenant_writer`.

## Why no per-search audit event

The endpoint sits in the picker's typing path. A hash-chained audit row
per keystroke is chain pollution, so this path is **metrics-only**
(`mdx_icd10_search_seconds`, `mdx_icd10_searches_total`) — the same
decision the sprint-10 autocomplete suggest path made. Recorded as a
deliberate deviation in the sprint-13 sign-off.
