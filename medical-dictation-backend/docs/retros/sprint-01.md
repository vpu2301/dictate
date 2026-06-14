# Sprint 01 Retrospective

**Window:** paved-road hardening (follows sprint-00 foundation)
**Reconstructed:** 2026-06-14
**Participants:** ___________

Sprint 01 is reconstructed as-built, so this retro is partly forward-
looking: the prompts below check whether the enforcement layer held up
across sprints 02–10 (and batches A/B), since the real test of a guardrail
is whether anyone routed around it. Answer in 2-4 sentences; non-
occurrences are interesting too.

---

## 1. Did the two custom gates earn their keep — or fire false positives (E3)?

> `no-os-environ` and `no-direct-asyncpg` were meant to make the unsafe
> path uncompilable. Did either block legitimate code and force an
> exclude/escape hatch? Did either catch a real mistake in 02–10?

> _Your answer_

---

## 2. Did anyone bypass hooks with `--no-verify` (E1)?

> The bet was that `make ci` + CI make local bypass harmless. Did a
> bypassed commit ever reach a PR red, and did CI catch it every time?

> _Your answer_

---

## 3. Was the import-boundary policy worth the friction (E4)?

> services↛services and libs↛services. Did a sprint ever *want* a
> cross-service import and have to refactor to HTTP/queue instead? Did
> the contract slow CI noticeably as the graph grew to 8+ services?

> _Your answer_

---

## 4. Did `make ci == CI` actually hold (E2)?

> The contract was byte-for-byte parity. Did local-green-CI-red drift
> ever happen — version skew, a CI-only gate (check-rls, openapi-check),
> a missing custom gate? How fast was it fixed?

> _Your answer_

---

## 5. Did "copy `_template`" survive contact with real services (E5)?

> Eight services were built after this. Did any hand-roll health/config/
> logging instead of inheriting the template? Did the template need a
> breaking change that forced a fan-out edit across copies?

> _Your answer_

---

## 6. gitleaks + commitizen — signal or noise (E6)?

> Did gitleaks flag historical test fixtures and need an allowlist? Did
> commitizen's Conventional-Commit gate stick, or did people fight it?

> _Your answer_

---

## 7. The `lint-imports` / `make ci` split (delta §12.1)

> `lint-imports` ended up a separate target, not part of `make ci`. Did a
> developer ever merge a boundary violation locally because `make ci`
> didn't catch it, leaving CI to reject it? Should it be folded in?

> _Your answer_

---

## 8. Onboarding friction loop never got a file (delta §12.2)

> `docs/onboarding-friction.md` was specified but not created. Did the
> 30-minute target hold without it, or did friction accumulate
> unrecorded? Is the loop worth backfilling now?

> _Your answer_

---

## Decisions to carry forward

- The two custom gates, the import-linter contracts, and the hardened
  `services/_template` are load-bearing and proven across 02–10 — do not
  relax without an ADR amendment.
- `make ci` as the local CI mirror stays the contract; any CI-only gate
  (check-rls, openapi-check) must be reachable via `make ci-with-db`.
- _Item 3_

## Decisions to revisit

- Fold `lint-imports` into `make ci`, or document the split as
  deliberate (delta §12.1).
- Create `docs/onboarding-friction.md` or remove the friction-loop claim
  from the spec (delta §12.2).
- _Item 3_
