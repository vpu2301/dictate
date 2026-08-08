# Corpus licensing register

Every corpus source is registered here BEFORE bulk ingestion; unlisted or
unclear licensing parks documents as `license_class='restricted'`, which the
snapshot builder excludes (constants.ALLOWED_SNAPSHOT_LICENSES) with an
alert. The operator-facing sourcing plan lives at
`~/Desktop/evidence-corpus-integration-plan.md` (to be moved into this repo
when the bulk load runs).

| Source | License basis | `license_class` | Verified | Notes |
|---|---|---|---|---|
| EVA-S02 test fixtures (generated) | authored in-repo | `public_domain` | 2026-08-05 | synthetic content, safe to redistribute |
| WHO IRIS guidelines | CC BY-NC-SA 3.0 IGO (per-doc) | `licensed_internal` | pending per-doc | internal clinical use OK; no redistribution |
| МОЗ України clinical protocols | normative acts (ст. 10 ЗУ «Про авторське право») | `public_domain` | pending per-doc | annexes may embed copyrighted scales — check each |
| PubMed abstracts | NLM terms, attribution | `licensed_redistributable` | pending | per-record copyright respected |
| PMC Open Access subset | CC BY / CC BY-NC per article | map per article | pending | `oa_noncomm` → `licensed_internal` |
| openFDA drug labels (SPL) | US public domain | `public_domain` | pending | drug_reference partition |
| NICE guidelines | syndication agreement required | `restricted` | — | NOT in snapshots until signed |
| Cochrane abstracts | quotable w/ attribution | `licensed_internal` | pending | full text stays out |

Rules:
- Adding a source = a row here + `--set license_class=...` on its ingest run.
- Lowering the admission bar (constants.ALLOWED_SNAPSHOT_LICENSES) is an
  ADR + legal review, not an edit.
