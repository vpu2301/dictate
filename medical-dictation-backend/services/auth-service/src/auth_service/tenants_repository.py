"""SQL for tenants (clinic profile / branding) and tenant memberships.

Two connection roles are used, mirroring the rest of auth-service:

* ``app_role`` (RLS-scoped via ``tenant_connection``) for reads of the
  caller's *active* tenant and its member roster.
* ``tenant_writer`` (unrestricted, ``USING (true)`` policies) for tenant
  creation/lifecycle, all membership writes, and the cross-tenant
  "which tenants can this principal reach" lookup — the one query that
  must span tenants and therefore cannot run under RLS.

Every function takes an already-acquired connection; the router owns the
pool choice so the isolation contract stays visible at the call site.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg

# Columns surfaced by the tenant API (excludes the raw ``logo_bytes`` blob,
# which is served by a dedicated endpoint).
TENANT_COLUMNS = """
    id, name, display_name, legal_name, slug, locale, timezone, status,
    is_active, logo_url, logo_content_type, contact_email, phone_number,
    website, address_line1, address_line2, postal_code, city,
    state_or_region, country, tax_id, registration_number,
    created_at, updated_at
"""

# Whitelisted columns the PATCH endpoint may set (router maps request → these).
UPDATABLE_TENANT_COLUMNS = frozenset(
    {
        "display_name",
        "legal_name",
        "slug",
        "locale",
        "timezone",
        "status",
        "is_active",
        "logo_url",
        "contact_email",
        "phone_number",
        "website",
        "address_line1",
        "address_line2",
        "postal_code",
        "city",
        "state_or_region",
        "country",
        "tax_id",
        "registration_number",
    }
)


# ── Tenants ──────────────────────────────────────────────────────────────


async def get_tenant(conn: asyncpg.Connection, *, tenant_id: UUID) -> asyncpg.Record | None:
    return await conn.fetchrow(
        f"SELECT {TENANT_COLUMNS} FROM tenants WHERE id = $1", tenant_id
    )


async def create_tenant(
    conn: asyncpg.Connection,
    *,
    name: str,
    display_name: str,
    slug: str,
    legal_name: str = "",
    locale: str = "uk",
    timezone: str = "Europe/Kyiv",
    contact_email: str = "",
    phone_number: str = "",
    website: str = "",
    address_line1: str = "",
    address_line2: str = "",
    postal_code: str = "",
    city: str = "",
    state_or_region: str = "",
    country: str = "",
    tax_id: str = "",
    registration_number: str = "",
) -> asyncpg.Record:
    return await conn.fetchrow(
        f"""
        INSERT INTO tenants
            (name, display_name, slug, legal_name, locale, timezone,
             contact_email, phone_number, website, address_line1, address_line2,
             postal_code, city, state_or_region, country, tax_id,
             registration_number, status, is_active)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14,
                $15, $16, $17, 'active', true)
        RETURNING {TENANT_COLUMNS}
        """,
        name,
        display_name,
        slug,
        legal_name,
        locale,
        timezone,
        contact_email,
        phone_number,
        website,
        address_line1,
        address_line2,
        postal_code,
        city,
        state_or_region,
        country,
        tax_id,
        registration_number,
    )


async def update_tenant(
    conn: asyncpg.Connection, *, tenant_id: UUID, fields: dict[str, Any]
) -> asyncpg.Record | None:
    """Patch a whitelist of tenant columns. The router owns the whitelist;
    callers never pass raw request keys through."""
    fields = {k: v for k, v in fields.items() if k in UPDATABLE_TENANT_COLUMNS}
    if not fields:
        return await get_tenant(conn, tenant_id=tenant_id)
    sets: list[str] = []
    args: list[Any] = []
    for col, val in fields.items():
        args.append(val)
        sets.append(f"{col} = ${len(args)}")
    args.append(tenant_id)
    return await conn.fetchrow(
        f"""
        UPDATE tenants
        SET {", ".join(sets)}, updated_at = now()
        WHERE id = ${len(args)}
        RETURNING {TENANT_COLUMNS}
        """,
        *args,
    )


async def set_logo(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    logo_bytes: bytes | None,
    logo_content_type: str,
    logo_url: str,
) -> asyncpg.Record | None:
    return await conn.fetchrow(
        f"""
        UPDATE tenants
        SET logo_bytes = $2, logo_content_type = $3, logo_url = $4,
            updated_at = now()
        WHERE id = $1
        RETURNING {TENANT_COLUMNS}
        """,
        tenant_id,
        logo_bytes,
        logo_content_type,
        logo_url,
    )


async def get_logo(
    conn: asyncpg.Connection, *, tenant_id: UUID
) -> tuple[bytes, str] | None:
    row = await conn.fetchrow(
        "SELECT logo_bytes, logo_content_type FROM tenants WHERE id = $1",
        tenant_id,
    )
    if row is None or row["logo_bytes"] is None:
        return None
    return bytes(row["logo_bytes"]), row["logo_content_type"] or "application/octet-stream"


# ── Memberships ──────────────────────────────────────────────────────────

MEMBERSHIP_COLUMNS = "id, tenant_id, user_sub, role, status, invited_by, created_at, updated_at"


async def list_tenants_for_user(
    conn: asyncpg.Connection, *, user_sub: UUID
) -> list[asyncpg.Record]:
    """All tenants the principal is a member of, with their membership role.

    Cross-tenant by design → must run on the ``tenant_writer`` pool (its
    ``USING (true)`` policies), never under app_role RLS.
    """
    return list(
        await conn.fetch(
            f"""
            SELECT {", ".join("t." + c.strip() for c in TENANT_COLUMNS.split(","))},
                   m.role AS membership_role, m.status AS membership_status
            FROM tenant_memberships m
            JOIN tenants t ON t.id = m.tenant_id
            WHERE m.user_sub = $1
            ORDER BY t.display_name, t.id
            """,
            user_sub,
        )
    )


async def get_membership(
    conn: asyncpg.Connection, *, tenant_id: UUID, user_sub: UUID
) -> asyncpg.Record | None:
    return await conn.fetchrow(
        f"SELECT {MEMBERSHIP_COLUMNS} FROM tenant_memberships "
        "WHERE tenant_id = $1 AND user_sub = $2",
        tenant_id,
        user_sub,
    )


async def list_members(
    conn: asyncpg.Connection, *, tenant_id: UUID
) -> list[asyncpg.Record]:
    """Roster of a tenant's members, joined to the local ``users`` row for
    display where available. LEFT JOIN so a cross-tenant member (whose
    ``users`` home row lives elsewhere) still lists, with null profile."""
    return list(
        await conn.fetch(
            """
            SELECT m.id, m.tenant_id, m.user_sub, m.role, m.status,
                   m.invited_by, m.created_at, m.updated_at,
                   u.email AS email, u.display_name AS display_name,
                   u.role AS platform_role, u.status AS user_status
            FROM tenant_memberships m
            LEFT JOIN users u ON u.sub = m.user_sub
            WHERE m.tenant_id = $1
            ORDER BY m.role, m.created_at
            """,
            tenant_id,
        )
    )


async def add_member(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    user_sub: UUID,
    role: str,
    invited_by: UUID | None,
    status: str = "active",
) -> asyncpg.Record:
    return await conn.fetchrow(
        f"""
        INSERT INTO tenant_memberships (tenant_id, user_sub, role, status, invited_by)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING {MEMBERSHIP_COLUMNS}
        """,
        tenant_id,
        user_sub,
        role,
        status,
        invited_by,
    )


async def update_member_role(
    conn: asyncpg.Connection, *, tenant_id: UUID, user_sub: UUID, role: str
) -> asyncpg.Record | None:
    return await conn.fetchrow(
        f"""
        UPDATE tenant_memberships
        SET role = $3, updated_at = now()
        WHERE tenant_id = $1 AND user_sub = $2
        RETURNING {MEMBERSHIP_COLUMNS}
        """,
        tenant_id,
        user_sub,
        role,
    )


async def remove_member(
    conn: asyncpg.Connection, *, tenant_id: UUID, user_sub: UUID
) -> bool:
    result = await conn.execute(
        "DELETE FROM tenant_memberships WHERE tenant_id = $1 AND user_sub = $2",
        tenant_id,
        user_sub,
    )
    # asyncpg returns e.g. "DELETE 1"
    return result.endswith(" 1")


async def count_active_owners(
    conn: asyncpg.Connection, *, tenant_id: UUID, exclude_sub: UUID | None = None
) -> int:
    n = await conn.fetchval(
        """
        SELECT count(*) FROM tenant_memberships
        WHERE tenant_id = $1 AND role = 'owner' AND status = 'active'
          AND ($2::uuid IS NULL OR user_sub <> $2)
        """,
        tenant_id,
        exclude_sub,
    )
    return int(n or 0)


async def resolve_sub_by_email(
    conn: asyncpg.Connection, *, email: str
) -> UUID | None:
    """Best-effort global email → sub lookup for adding an existing platform
    user by email. Runs on the ``tenant_writer`` pool (unrestricted). Email is
    unique per tenant, not globally; the deterministic ordering picks the
    oldest active match when a rare cross-tenant collision exists."""
    row = await conn.fetchrow(
        """
        SELECT sub FROM users
        WHERE lower(email) = lower($1)
        ORDER BY (status = 'active') DESC, created_at
        LIMIT 1
        """,
        email,
    )
    return row["sub"] if row is not None else None
