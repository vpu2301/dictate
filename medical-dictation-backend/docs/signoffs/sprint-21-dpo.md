# Sprint 21 — DPO sign-off: corpus mining + review pipeline

Status: **PENDING — mining must not run against production data until both
sections are countersigned.** (Dev-stack runs against synthetic seed data
are exempt.)

## 1. The mining query

Authoritative text: `MINING_QUERY` in
`services/corpus-forge/src/corpus_forge/adapters/mining.py`.

**SHA-256 of the signed text:**
`09eaddaf5a62bddf737f03b042a5b52a6f93fdb60d45afe1f9681e65af4c47eb`

Every `corpus.mining_run` audit event records the SHA of the query that
actually ran; a mismatch with the value above means the query changed after
sign-off and the run is out of policy. Editing `MINING_QUERY` invalidates
this document.

Privacy properties of the query (ADR-0043 §7):

- Reads only the **current version of finalized/signed reports** — drafts
  and patient tables are never n-gram sources.
- **k-anonymity in SQL**: an n-gram survives only with ≥5 distinct authors
  across ≥2 tenants (parameters; defaults recorded in every audit event).
- **Patient-name token rejection in SQL**: any n-gram containing any token
  of any patient's name (uk or en) is discarded.
- Post-SQL, in the same process: the 6-pattern PII screen **drops** (never
  redacts) matching n-grams; nothing is written to disk between the SQL
  read and the staging insert.
- Mining runs under a dedicated read-only DSN (`MDX_CORPUS_DSN`), never a
  tenant-scoped app connection (cross-tenant reads are the point of the
  k-anonymity gate). Production role setup: docs/runbooks/corpus.md.

Licence register for the terminology importers:
docs/sprint-21/EXPLORE.md §2 (reconstructed — `data-sources.md` never
landed in the repo; countersign covers that register too).

| Role | Name | Date | Signature |
| --- | --- | --- | --- |
| DPO | | | |
| Tech lead | | | |

## 2. The review pipeline

- Human decisions are recorded in the append-only, hash-chained
  `corpus_reviews` (migration 0083); the chain is verified per insert by a
  DB trigger, UPDATE/DELETE raise.
- LLM-jury decisions record `review_engine = jury:<model>:<prompt_version>`;
  PHI-derived candidates are judged only in-perimeter, enforced by a raise
  in code with a unit test (ADR-0044 §1).
- Tier-3 candidates (dose/drug/laterality/negation/ICD/abbreviation) are
  human-mandatory with no machine path.
- Auto-accept for tier 1 stays off until the calibration gate passes
  (docs/eval/sprint-21-jury-calibration.md, ≤2% false-accept).

| Role | Name | Date | Signature |
| --- | --- | --- | --- |
| DPO | | | |
| Clinical reviewer | | | |
