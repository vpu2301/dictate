"""Tenant branding for exported documents (PDF header / letters).

The branding data lives on the ``tenants`` row (extended in migration 0032).
report-service can read its *own* tenant under RLS (``tenants_self_select``),
so :func:`load_tenant_branding` takes an already tenant-scoped connection and
returns a small, PHI-free value object the PDF pipeline can consume.

This keeps the "prepare branding data" concern separate from the renderer:
today only ``issuer_name`` is threaded into the existing unsigned-PDF
template (so no golden-PDF churn), but the full :class:`TenantBranding` is
available for richer letterhead work and for the SPA's PDF-preview endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import asyncpg


@dataclass(frozen=True)
class TenantBranding:
    """Branding fields surfaced to document rendering. All optional/blank-safe."""

    tenant_id: str
    name: str = ""
    display_name: str = ""
    legal_name: str = ""
    logo_url: str = ""
    has_logo: bool = False
    contact_email: str = ""
    phone_number: str = ""
    website: str = ""
    address_lines: list[str] = field(default_factory=list)

    @property
    def issuer_name(self) -> str:
        """The name printed as the issuing organisation on a document.

        Prefers the registered legal name, falls back to the display name,
        then the machine name — never empty for a real tenant."""
        return self.legal_name or self.display_name or self.name or "—"

    @property
    def address_block(self) -> str:
        """Single-line postal address for a compact header."""
        return ", ".join(p for p in self.address_lines if p)


def _branding_from_row(row: asyncpg.Record) -> TenantBranding:
    address_lines = [
        row["address_line1"],
        row["address_line2"],
        " ".join(p for p in (row["postal_code"], row["city"]) if p),
        " ".join(p for p in (row["state_or_region"], row["country"]) if p),
    ]
    return TenantBranding(
        tenant_id=str(row["id"]),
        name=row["name"],
        display_name=row["display_name"],
        legal_name=row["legal_name"],
        logo_url=row["logo_url"],
        has_logo=bool(row["logo_content_type"]),
        contact_email=row["contact_email"],
        phone_number=row["phone_number"],
        website=row["website"],
        address_lines=[line for line in address_lines if line.strip()],
    )


async def load_tenant_branding(
    conn: asyncpg.Connection, *, tenant_id: str
) -> TenantBranding:
    """Load branding for the (RLS-scoped) tenant. Returns an empty-but-valid
    object if the row is somehow unreadable, so PDF rendering never fails on
    missing branding."""
    row = await conn.fetchrow(
        """
        SELECT id, name, display_name, legal_name, logo_url, logo_content_type,
               contact_email, phone_number, website, address_line1, address_line2,
               postal_code, city, state_or_region, country
        FROM tenants
        WHERE id = $1
        """,
        tenant_id,
    )
    if row is None:
        return TenantBranding(tenant_id=str(tenant_id))
    return _branding_from_row(row)
