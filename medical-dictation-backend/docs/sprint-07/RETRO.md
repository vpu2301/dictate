# Sprint 07 — Retro

## What went well

- **User course-correction on day-1 was decisive.** "but not demo
  auth, we csan provide real HF key" eliminated an entire module
  (`auth-service/demo/`) and let us reuse sprint-02's Keycloak
  unmodified inside the Space. Net cost: ~2 days saved; net quality
  gain: one auth surface, not two.
- **Privacy envelope mostly fell out of sprint-04 wiring.** The
  `disabled` flag on `ObjectStore` was a 30-line change. The daily
  privacy test is the same WS protocol as production — no special
  test surface.
- **WER pipeline determinism contract.** Hashing prompts +
  pipeline_version + model name into every `eval_runs` row means
  future bisect is trivial; the team unanimously accepted the
  "no silent rebaselining" rule (ADR-0019).
- **Three-axis rate limiter shipped with 100% test coverage on first
  attempt.** fakeredis carried the day.

## What was hard

- **HF Space cold-start (~25s) with Java + Keycloak.** Considered
  swapping to a lighter OIDC (Zitadel) but the carry cost of two auth
  flows would dominate any cold-start win. Accepted as-is; documented
  in the runbook.
- **Corpus authoring is the long pole.** v1 corpus only has placeholder
  fixtures shipping with this sprint. Linguist consultant + clinical
  content lead are authoring in parallel; ETA end of sprint-08 week 1.
- **First implementation of the rate limiter had non-async fixtures**
  that pytest 9 would have rejected. Caught by the test run; fixed
  with `pytest_asyncio.fixture`. Worth adding a linter rule.

## What we would change

- **Author the corpus first, then the pipeline.** We built `run_wer.py`
  against placeholder audio; small surprises (silence handling, empty
  hypothesis) are likely to surface once real recordings arrive.
- **Stage realm-export earlier.** Spent half a day on Keycloak
  schema/version mismatch. A CI lint that boots Keycloak with the
  staged realm-export against the pinned image would have caught it.

## Decisions taken

- Privacy envelope is the demo's contract — codified in ADR-0018.
- WER is now a standing release gate — ADR-0019.
- Embedded stack pattern (Postgres + Redis + Keycloak in one
  container) is **demo-only**. Production retains the separated
  topology. ADR-0017.

## Carry-over items

- Sprint-08: load test harness, full corpus authoring, WER baseline
  capture after 3 consecutive runs.
- Sprint-08: add a hard "demo-mode envvars are off in prod images" CI
  gate, just in case.
