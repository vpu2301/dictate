# Sprint 08 — Retro

## What went well

- **Day-1 data model held up.** The deferrable-FK two-step insert
  worked exactly as planned; no need for the alternate (allow NULL
  permanently + nightly check). The chicken-egg is now a documented,
  bounded sharp edge.
- **`extra='forbid'` on every Pydantic model.** Caught two unrelated
  bugs during integration testing where extra keys were quietly
  dropped — sprint-09 signing will be more reliable as a result.
- **Hypothesis property test composes the same verifier as the
  reconciler.** A single source of truth for chain rules: the CI
  property test re-runs the production verifier, so a fix to one
  immediately strengthens the other. This was directly modelled on
  sprint-02's audit verifier pattern.
- **Aggregated `report.draft.updated`.** Buffering autosaves by
  session avoided an audit volume blow-up that would have made
  sprint-09 signing of audit chain blocks much more expensive.

## What was hard

- **Hypothesis strategy for valid chains had two bugs**: genesis
  forgot to set `last_signed_idx` when n_pre_sign=1; amendments
  initially modelled parents wrong (off the original signed parent
  instead of the immediate previous version). Both were caught on
  the first run.
- **The `pytest-asyncio` plugin rejected `pytestmark` on modules
  with sync tests.** Easy fix (drop the module-level marker), but
  worth a lint rule.
- **Postgres `simple` FTS** quality on Ukrainian morphology is
  measurably worse than `russian` config would be — but
  Ukrainian-correct linguistics needs a contractor. ADR-0021 names
  the trigger condition.

## What we would change

- **Author the load test against a real DB earlier.** We have the
  seed + k6 scaffolding but the actual baseline hasn't been captured
  yet (no pilot DB available this sprint). Sprint-09 has to allocate
  a half-day to run + record the baseline before its first KEP push.
- **Surface read-purpose to the FE earlier.** FE was surprised by
  the 422-without-purpose path. Documented in the frontend-aligned
  patch but worth a protocol-sync session in sprint-09 kick-off.

## Decisions taken

- Append-only versioning is the contract for every clinical artifact
  going forward (ADR-0020).
- `simple` FTS is good-enough for sprint-08; trigger conditions for
  upgrade are documented (ADR-0021).
- Aggregated `report.draft.updated` is the new audit volume pattern
  for high-frequency events (the autosave→session aggregation is the
  reference implementation).
- `chain_integrity.verify_chain` is the single canonical verifier;
  CI + reconciler call the same function.

## Carry-over items

- Sprint-09: KEP signing + `report.amended` audit kind owned by
  signing.
- Sprint-16: idle-draft cleanup scheduler; partition implementation
  if triggered.
- Sprint-15: search tips UI + (optional) query expansion.
- Sprint-17: FHIR Composition export uses `reports.icd10_codes` and
  the template's `fhir_template` metadata.
