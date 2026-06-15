# Sprint 00 Retrospective

**Window:** foundation (ground floor)
**Reconstructed:** 2026-06-14
**Participants:** ___________

Sprint 00 is reconstructed as-built, so this retro is partly forward-
looking: the prompts below check whether the foundation held up across
sprints 01–10 (and batches A/B), since that is the real test of a paved
road. Answer in 2-4 sentences; non-occurrences are interesting too.

---

## 1. Did "copy `_template`" actually keep services uniform?

> Did any service hand-write health / config / logging / tracing
> instead of inheriting it? Template drift (risk E4) is the thing to catch.

> _Your answer_

---

## 2. Was `Secret[T]` ergonomic, or did people fight `.reveal()`?

> Friction here pushes engineers toward leaking plaintext. Worth it?

> _Your answer_

---

## 3. Did `PIISafeFilter`'s fixed field set need extending?

> Each PHI-adding sprint should have reviewed it (risk E3). Did the list
> grow when transcript / audio / patient columns arrived in 03–08?

> _Your answer_

---

## 4. Did `tenant_connection` survive as the *only* tenant-scoped path?

> Sprint 02 added exactly one escape hatch (audit_writer). Did any other
> code bypass it for performance, and did RLS catch it anyway?

> _Your answer_

---

## 5. `uv` workspace — net positive or bus-factor risk (E1)?

> Lockfile determinism, single-venv ergonomics vs. tooling maturity.
> Any moment we wished we'd used plain pip/poetry?

> _Your answer_

---

## 6. CI gate cycle time

> Did lint + mypy --strict + pytest + security + import-linter +
> container-scan stay fast enough that nobody routed around it?

> _Your answer_

---

## 7. Distroless + nonroot debugging friction (E7)

> When a service misbehaved, did the no-shell image slow diagnosis? Was
> the sidecar/ephemeral-container workflow actually used?

> _Your answer_

---

## 8. The reconstruction itself

> The canonical spec lived in conversation history, not the repo, until
> 2026-06-14. What else is load-bearing but undocumented in-repo?

> _Your answer_

---

## Decisions to carry forward

- Foundation primitives (`Secret[T]`, `tenant_connection`, observability
  bootstrap, service template, import-linter contracts) are stable and
  proven across 01–10 — do not relax without an ADR amendment.
- _Item 2_

## Decisions to revisit

- KMS-backed key management (deferred to sprint-16; file-based since 03).
- _Item 2_
