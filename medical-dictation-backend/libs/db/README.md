# `libs/db` — pool + tenant-scoped connection

Two layers:

- `make_engine` / `Base` — async SQLAlchemy engine factory and shared
  declarative base for ORM-driven services.
- `create_pool` / `tenant_connection` — asyncpg pool plus the **single
  sanctioned way** to acquire a row-level-security-scoped DB connection.

```python
from db import create_pool, tenant_connection

pool = await create_pool(dsn, application_name="encounters-service")

async with tenant_connection(pool, tenant_id) as conn:
    rows = await conn.fetch("SELECT * FROM encounters")
    # ↑ RLS guarantees rows belong to tenant_id
```

## Why a single helper

Every tenant-scoped query must execute inside a transaction whose first
statement sets `app.tenant_id` so the RLS policies in `audit.*` and `public.*`
can filter rows. Letting callers do this themselves means one missed call =
silent cross-tenant leak. We expose only `tenant_connection`; there is no
escape hatch in Sprint 01. Sprint 02 introduces exactly one documented
exception (the audit writer, ADR-0008) using a `ContextVar` flag — no other
escape hatches without an ADR amendment.

## Why `set_config(..., true)` not `SET LOCAL`

Postgres rejects parameter binding inside `SET LOCAL`. `set_config(name,
value, is_local=true)` accepts parameters and is transaction-local — the
setting is cleared automatically at COMMIT or ROLLBACK, so a connection
returned to the pool cannot leak the previous tenant's identity into the
next acquirer.

## Why `statement_cache_size=0`

Production places this pool behind pgbouncer/pgcat in transaction-pooling
mode (Sprint 16). Prepared statements cached by asyncpg become invalid in
that topology. Disabling the cache costs a few microseconds per query in dev
and unblocks the Sprint 16 migration without code changes.

## Tests

- `tests/unit/test_tenant_validation.py` — input validation (UUID coercion,
  SQLi payload rejection).
- `tests/integration/test_tenant_isolation.py` — round-trip against the dev
  Compose Postgres. Run via `RUN_DB_INTEGRATION=1 uv run pytest libs/db`.

## See also

- ADR-0004 — RLS tenant connection helper.
- `infra/postgres/init.sql` — creates `app_role`, `audit_writer`,
  `audit_reader`, and the `audit` schema.
