"""Unit tests for the tenant branding helper used by the PDF pipeline."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from report_service.domain.branding import (
    TenantBranding,
    _branding_from_row,
    load_tenant_branding,
)

NOW = datetime(2026, 6, 1, tzinfo=UTC)


def _row(**over):
    base = {
        "id": uuid4(),
        "name": "tenant-a",
        "display_name": "Dev Hospital A",
        "legal_name": "Dev Hospital A LLC",
        "logo_url": "https://cdn/logo.png",
        "logo_content_type": "image/png",
        "contact_email": "contact@a.example",
        "phone_number": "+380",
        "website": "https://a.example",
        "address_line1": "1 Khreshchatyk St",
        "address_line2": "",
        "postal_code": "01001",
        "city": "Kyiv",
        "state_or_region": "",
        "country": "Ukraine",
    }
    base.update(over)
    return base


def test_issuer_name_prefers_legal_name() -> None:
    b = _branding_from_row(_row())
    assert b.issuer_name == "Dev Hospital A LLC"


def test_issuer_name_falls_back_to_display_then_name() -> None:
    assert _branding_from_row(_row(legal_name="")).issuer_name == "Dev Hospital A"
    assert _branding_from_row(_row(legal_name="", display_name="")).issuer_name == "tenant-a"


def test_issuer_name_dash_when_all_blank() -> None:
    b = _branding_from_row(_row(legal_name="", display_name="", name=""))
    assert b.issuer_name == "—"


def test_address_block_joins_non_empty_parts() -> None:
    b = _branding_from_row(_row())
    assert b.address_block == "1 Khreshchatyk St, 01001 Kyiv, Ukraine"
    assert b.has_logo is True


def test_empty_branding_is_valid() -> None:
    b = TenantBranding(tenant_id="t")
    assert b.issuer_name == "—"
    assert b.address_block == ""
    assert b.has_logo is False


@pytest.mark.asyncio
async def test_load_tenant_branding_missing_row_is_safe() -> None:
    class _Conn:
        async def fetchrow(self, *a, **k):  # noqa: ANN002, ANN003
            return None

    b = await load_tenant_branding(_Conn(), tenant_id="00000000-0000-0000-0000-0000000000ff")
    assert b.issuer_name == "—"
