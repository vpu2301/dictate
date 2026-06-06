# ADR-0007 — RLS-First Tenant Isolation

**Date:** 2026-05-11
**Status:** Accepted
**Deciders:** Backend tech lead, Security lead, Database SME
**Builds on:** ADR-0004 (tenant_connection helper)

---

## Context

Multi-tenant SaaS leaks tenant data when application code forgets a
`WHERE tenant_id = ?` clause exactly once. The cost is catastrophic and
the failure mode is silent — the query returns rows; only the customer
who eventually sees them knows there was a breach.

ADR-0004 introduced `tenant_connection(pool, tenant_id)` as the *single*
way to obtain a connection that's scoped via `app.tenant_id`. Sprint 02
takes the next step: enforce isolation **in the database**, not the
application. Service code never writes `WHERE tenant_id = ?` by hand;
Postgres row-level security does it implicitly.

## Decision

Every table that stores tenant data:

1. Has a `tenant_id UUID NOT NULL` column (or `id` for the `tenants`
   table itself).
2. Has `ENABLE ROW LEVEL SECURITY` AND `FORCE ROW LEVEL SECURITY`. The
   FORCE is non-negotiable — without it, the table owner (often
   `postgres`) bypasses RLS even when the policies are present.
3. Has at minimum a **PERMISSIVE** policy granting access to rows where
   `tenant_id = current_setting('app.tenant_id', true)::uuid`.
4. SHOULD have a **RESTRICTIVE** policy enforcing the same predicate as
   defence in depth (Sprint 02's `users` and `audit.events` tables do).

Service code never filters by `tenant_id`. The `app_role` Postgres role
is non-superuser, has no `BYPASSRLS`, and runs every query through
`tenant_connection` which sets `app.tenant_id` per-transaction.

The CI gate `scripts/ci/check-rls-policies.py` (introduced in Day 9)
queries `pg_class` after every migration and fails CI if any user-schema
table lacks `relrowsecurity AND relforcerowsecurity`.

## Consequences

**Positive**

- A developer adding a new tenant-scoped table without writing a policy
  gets a CI failure, not a silent leak.
- Forgetting a `WHERE` clause yields the correct (empty) result for the
  wrong tenant. The blast radius of a single bug is one query, not the
  whole table.
- Auditors can verify isolation by `EXPLAIN`-ing a query and seeing the
  RLS-injected filter, not by reading every service's data layer.
- A hypothesis property test (`libs/db/tests/integration/test_rls_isolation.py`)
  generates 50 random tenant/user layouts and asserts cross-tenant
  queries return zero rows. Catches policy-mistakes mechanically.

**Negative**

- Performance: every query gets the RLS predicate appended. For tables
  with a `tenant_id` index this is negligible; for cross-cutting
  analytics it requires a separate elevated-role code path.
- Operational footguns: `pg_dump` dumps without RLS by default. Backups
  must be encrypted-at-rest and access-controlled with the same rigour
  as the live DB.
- Policies stack — adding a too-loose PERMISSIVE policy can defeat
  isolation even when RESTRICTIVE is present. Code review + the CI gate
  + the property test triangulate.

## The escape hatches we sanctioned

Sprint 02 introduces exactly two roles that operate outside the
`tenant_connection` pattern, both documented:

- `tenant_writer` — used only by auth-service for tenant CRUD. The
  `tenants` table has a PERMISSIVE policy `FOR ALL TO tenant_writer`
  with `USING(true)` since tenant *creation* has no incumbent tenant.
  For `users` the role still honours `app.tenant_id`.
- `audit_writer` — see ADR-0008. Required because the audit chain needs
  an INSERT-only role with no `app.tenant_id` ambiguity.

New escape hatches require an ADR amendment.

## Alternatives considered

- **App-layer filters** — every query reads `tenant_id` from claims and
  appends `WHERE tenant_id = ?`. Rejected: forgetting once is fatal; the
  testing burden is N×M for N tables × M queries.
- **Separate database per tenant** — strongest isolation. Rejected:
  doesn't scale to thousands of tenants, makes cross-tenant analytics
  impossible, complicates schema migrations.
- **Separate schema per tenant** — middle ground. Rejected: same scale
  ceiling, harder schema evolution, doesn't compose with the audit log's
  per-tenant chains.
- **No tenancy in the DB; isolation in the API gateway** — rejected:
  bypasses the DB if anyone ever needs direct SQL access (BI, support
  ticket forensics, etc.).

## Trigger conditions for revisiting

- Postgres-level RLS performance becomes a measurable cost at the p95.
- We need a SaaS analytics surface that legitimately spans tenants — at
  which point we add a third explicit escape-hatch role with an ADR.
- We adopt a transaction-mode connection pooler that breaks the
  `set_config(..., true)` mechanism. (asyncpg's current pool is
  prepared-statement-cache-disabled to anticipate pgbouncer in
  transaction mode.)
