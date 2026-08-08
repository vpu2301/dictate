# EVA-S01 — Retro (2026-08-04)

Prompts keyed to the sprint's risks.

**R1 — building on a missing S00.** The biggest finding: S01 was specced on an
S00 that was never built. Resolution (minimal S00 bootstrap + ADR-0001) worked,
but the outstanding S00 items (model gateway, dictat evidence module, shells,
pre-commit suite, batch-A harness) are now unowned. → Carried to SPRINT-TODO;
must land before or with S02, or Batch A's exit review has nothing to review.

**R2 — enum freeze without a clinical advisor in the loop.** The "enum workshop
with the clinical advisor" (spec D1–2) had no advisor available; values were
chosen from the framework rules and recorded in ADR-0002 with the sign-off row
left open. Right call vs inventing a fake sign-off — but the tag must wait for
the countersignature, and a value-set change after S02 consumes the enums gets
expensive fast. → Chase the sign-off before S02 starts.

**R3 — two-workspace coupling.** Permissions, migrations, Keycloak and seeds
live in the platform tree; contracts and gates live here. `make ci` green in
one workspace says nothing about the other. → Follow-up: an aggregate target
(or CI job) that runs both, so a sprint can't declare done on half the surface.

**What went well.** The recon-first approach (reading the platform's actual RLS
idiom, GUC precedent, drift-test mechanics before writing) prevented three
spec-vs-reality collisions (app.user_sub, immutable-trigger reuse, users.role
CHECK) from becoming runtime surprises; the negative-proof discipline caught
nothing broken — which is the point of running it while it's cheap.

**Metric.** 23 unit + 9 DB-integration tests, 5 gates, 2 ADRs, 1 migration,
97 CSV rows — one working day with the platform stack already up.
