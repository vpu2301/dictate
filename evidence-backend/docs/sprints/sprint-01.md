# EVA-S01 — Backend — Domain Model & Contracts

**Objective:** Freeze v1 of every core contract in `libs/evidence_models` (Pydantic v2, JSON Schema export, dictat typegen), create the evidence Postgres schema with RLS + FORCE and append-only provenance, register all evidence **actions** in the shared permission matrix, and lock the additive-only contract policy in CI. No pipeline logic; no models invoked.
**Depends on:** S00. **Hands off to:** S02 (document/chunk schema), S04+ (envelope), S05 (snapshot).

*(Sections 1–13 as delivered in the framework sprint file — the framework copy at
`~/Desktop/evidence-framework/docs/sprints/sprint-01-domain-model.md` remains the
planning source; this file records what was actually built and where it diverged.)*

---

## As-built (2026-08-04)

### What shipped, where

| Deliverable | Location | Proof |
|---|---|---|
| Contracts v1 (14 schemas) | `libs/evidence_models/src/evidence_models/` | 11 unit tests; `make ci` green |
| JSON Schema export | `docs/api/evidence-contracts/*.v1.schema.json` (+ `CHANGELOG.md`) | `make contracts-dump` / `contracts-check` |
| Additive linter | `scripts/contracts/lint_additive.py` | 12 unit tests (both directions) |
| Leaf gate | `scripts/dev/check_leaf_imports.py` (`make check-leaf-imports`) | negative proof: planted `import asyncpg` fails the gate |
| dictat typegen | dictat `scripts/contracts-typegen.mjs` → `src/types/evidence.d.ts` (134 types); npm scripts `typegen:evidence`, `verify:evidence-types` | `tsc --noEmit --strict` green |
| Evidence schema | platform migration `0067_evidence_domain.sql` (+`.down.sql`) — 11 tables, RLS+FORCE+RESTRICTIVE, HNSW on `chunks.embedding`, `users.role` CHECK extended | up→down→up cycle green; platform `check-rls` PASS (48 tables); `check-erasure-fanout` PASS |
| Append-only provenance | `evidence_provenance_immutable()` trigger + grants | UPDATE/DELETE denied for `app_role` AND for a granted superuser (trigger layer) |
| Dual-key question privacy | RLS on `questions`/`answers` adds `user_sub = current_setting('app.user_id', true)::uuid` | same-tenant other-user reads zero rows |
| Permission matrix | platform `libs/auth/perms.py` + `permissions.csv`: 9 `evidence.*` actions, target kinds `evidence`/`evidence_corpus`, role `knowledge_admin` (explicit false on all 43 pre-existing pairs) | bidirectional drift test 18/18 green; negative proof: removed CSV row fails 2 tests |
| Keycloak + seed | realm role `knowledge_admin`; users `dev-knowledge`(-b) = `knowledge_admin@tenant-{a,b}.example` / `dev-password`; `seed.sql` user rows | `make seed` green; DB test asserts both users |
| ADRs | `docs/adr/0001` (workspace placement), `docs/adr/0002` (segment taxonomy — the spec's "ADR-000C") | committed |

### As-built deltas (flagged to sign-off, not smoothed over)

1. **S00 was never built.** This sprint bootstrapped the minimal S00 foundation
   it needed (uv workspace, Makefile/`make ci`, gates, docs scaffold) and
   recorded the placement decision as ADR-0001. Consequences:
   - no `evidence_probe` table existed, so the spec's "S00 probe dropped here"
     is a no-op; the probe audit kind was never registered (noted in the
     platform audit catalogue);
   - S00's model gateway, dictat evidence module, and both shells remain
     outstanding — they block S02+ runtime work, not contracts.
2. **GUC name:** the spec's `app.user_sub` does not exist anywhere in the
   platform; the dual-key RLS uses the platform's existing per-user GUC
   precedent **`app.user_id`** (autocomplete-service). Column stays `user_sub`.
3. **`answer_provenance.answer_id` is deliberately not an FK** — provenance
   must survive erasure of the answer row (rule PR1 pseudonymization; the
   audit-events precedent). A hard FK would either block erasure or be
   destroyed by it.
4. **Immutability trigger is evidence-specific.** `audit.events_immutable()`
   is column-coupled (`OLD.seq`); reusing it would raise a different, uglier
   error. `evidence_provenance_immutable()` clones the pattern.
5. **`users.role` CHECK extension** rides inside 0067 (the spec did not call
   it out; without it `knowledge_admin` rows are uninsertable). The down
   migration restores the narrow CHECK and documents that seeded
   knowledge-admin users must be removed first.
6. **Leaf-ness gate is an AST script**, not an import-linter contract:
   import-linter cannot express "forbidden module that is not installed in
   this venv". Same rule, sturdier mechanism.
7. **Contract release tag not yet created:** `evidence-contracts-v1.0.0` must
   be tagged on the merge commit (procedure in the contracts CHANGELOG). Until
   then the additive linter reports "first release" and passes; its
   classification logic is pinned by 12 unit tests.
8. **Internal `GET /contracts/schema/{name}@{version}`** ships with
   evidence-answer (S04+) as specced; this sprint ships the export artifact
   only.
9. **`checks_results.entities`** stays `jsonb` (spec) while the wire
   `CheckResult.entities` is `list[str]` — narrower on the wire is additive-safe.

### Verification protocol — results

| # | Proof | Result |
|---|---|---|
| 9.1 | Round-trip: fixtures (all six kinds) → JSON → exported-schema validation → dictat typegen strict-compiles | ✅ 11 unit tests + `tsc` green |
| 9.2 | ET2 negative: uncited evidence segment → ValidationError; DB CHECK rejects the same at storage | ✅ unit + DB test |
| 9.3 | RLS crafted probes: cross-tenant zero rows on all 11 tables; unscoped connection zero rows; injected tenant_id insert rejected; same-tenant other-user question read zero rows | ✅ 4 DB tests |
| 9.4 | Append-only: UPDATE/DELETE denied at grant layer (app_role) and trigger layer (superuser) | ✅ 3 DB tests |
| 9.5 | Linter both directions: 7 breaking classes detected, 5 additive classes pass (incl. major-bump path) | ✅ 12 unit tests; live scratch-branch run deferred to the tagged release (delta 7) |
| 9.6 | Permissions drift both directions: removed CSV row → 2 tests fail; restored → 18/18 | ✅ live negative run |
| 9.7 | Migration up→down→up on the live dev DB; `check-rls` + `check-erasure-fanout` green after | ✅ live run |

### Acceptance criteria

- **AC-S01-B-1** ✅ contracts exported; dictat `evidence.d.ts` strict-compiles (tag on merge = delta 7)
- **AC-S01-B-2** ✅ ET2 by validator + DB CHECK, negative-tested
- **AC-S01-B-3** ✅ RLS + append-only proofs incl. crafted probes (9 DB tests)
- **AC-S01-B-4** ✅ additive linter proven both directions (unit level; live vs tag after release)
- **AC-S01-B-5** ✅ matrix extended, drift test green, `knowledge_admin` seeded (Keycloak + DB) and matrix-usable
- **AC-S01-B-6** ⏳ clinical sign-off on the enum set — ADR-0002 carries the decision table; sign-off register row open
