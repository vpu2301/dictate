# ADR-0004 — Single-Helper Tenant Connection (`tenant_connection`)

**Date:** 2026-05-09
**Status:** Accepted
**Deciders:** Backend tech lead, Security lead, Database SME

---

## Context

The platform is multi-tenant. Tenant isolation is enforced at the database
layer via Postgres row-level security (RLS): every table that holds tenant
data carries a `tenant_id` column and an RLS policy of the form

```sql
USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
```

For the policy to do its job, every connection that touches tenant data
must execute `SET app.tenant_id = ...` *before* the first query — and
*after* the previous tenant's value is cleared. A pool reuses connections
across tenants, so this is not a one-time setup; it is a per-acquire
contract. Missing the contract once equals a silent cross-tenant leak.

## Decision

`libs/db` exposes **exactly one** way to obtain a tenant-scoped connection:

```python
async with tenant_connection(pool, tenant_id) as conn:
    ...
```

The helper:

1. Validates `tenant_id` is a UUID (rejects strings that look like SQLi).
2. Acquires a connection from the pool.
3. Begins a transaction.
4. Sets `app.tenant_id` via `SELECT set_config('app.tenant_id', $1, true)`.
   The third argument (`true`) makes it transaction-local; the value is
   automatically cleared at COMMIT or ROLLBACK so the connection cannot
   leak the previous tenant's identity to the next acquirer.
5. Yields the connection.
6. Commits on clean exit; rolls back on exception.

There is **no escape hatch** in Sprint 01. Sprint 02 introduces exactly
one sanctioned exception (the audit writer, which writes platform-level
events with no tenant scope) using a `ContextVar` flag — documented as the
only exception. New escape hatches require an ADR amendment.

The pre-commit hook `check-no-direct-asyncpg.py` rejects `asyncpg.connect`
or `asyncpg.create_pool` outside `libs/db/`.

## Consequences

**Positive**

- One audit point. Reviewers always know where tenant scoping happens.
- Test pattern is symmetric: integration tests assert that a connection
  acquired with tenant B sees zero rows inserted by tenant A.
- The transaction-local config makes pool reuse safe.

**Negative**

- Long-running migrations that need cross-tenant access must use a
  separate, super-user role and a separate code path. We accept that.
- The contract is enforceable only by review + lint, not by the type
  system. The lint hook is the safety net.

## Why `set_config(..., true)` not `SET LOCAL`

Postgres rejects parameter binding inside `SET LOCAL`. `set_config(name,
value, is_local=true)` accepts parameters, is transaction-local, and
clears at COMMIT or ROLLBACK. Using SQL string interpolation to inject a
tenant ID is exactly the kind of footgun this ADR exists to prevent.

## Trigger conditions for revisiting

- We adopt connection-pooling middleware (pgbouncer / pgcat) in
  transaction mode (Sprint 16). The current pool already disables
  prepared-statement caching to anticipate this.
- A second sanctioned escape hatch is needed. Threshold: an ADR.
