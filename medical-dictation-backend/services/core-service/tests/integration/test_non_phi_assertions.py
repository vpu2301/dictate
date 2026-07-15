"""S11 step 05 §4.3 — pin the "confirmed non-PHI by convention" claims.

The fan-out map's KNOWN_NON_PHI allowlist asserts that
``autocomplete_telemetry`` and ``audit.events`` never carry patient
identity strings. These tests pin that with data instead of trusting
comments — if either fails, that is a REAL LEAK, not a test to soften.

Skipped unless ``RUN_DB_INTEGRATION=1``.
"""

from __future__ import annotations

import os

import asyncpg
import pytest

from .test_fanout import MARK, SU_DSN, build_fixture_patient_with_everything, cleanup_fixture

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DB_INTEGRATION") != "1",
    reason="set RUN_DB_INTEGRATION=1 to run; needs `make dev-up && make migrate-up && make seed`",
)

# A syntactically valid ІПН (synthetic) planted on the fixture patient in
# hmac form only — the raw string below must never appear anywhere.
SYNTHETIC_IPN = "1759013776"


async def test_telemetry_and_audit_carry_no_identity_strings() -> None:
    su = await asyncpg.connect(SU_DSN)
    try:
        await cleanup_fixture(su)
        tenant_id, patient_id, _ids = await build_fixture_patient_with_everything(su)

        # (a) autocomplete telemetry: scrubbed prefixes + ids only — the
        # fixture patient's marker name and the ІПН shape never appear.
        telemetry_hits = await su.fetchval(
            "SELECT count(*) FROM autocomplete_telemetry "
            "WHERE prefix_scrubbed ILIKE $1 OR prefix_scrubbed LIKE $2 "
            "OR context_jsonb::text ILIKE $1 OR context_jsonb::text LIKE $2",
            f"%{MARK}%",
            f"%{SYNTHETIC_IPN}%",
        )
        assert telemetry_hits == 0, (
            "REAL LEAK: patient identity found in autocomplete_telemetry — "
            "the S10 scrubber contract is broken; STOP and investigate"
        )

        # (b) audit payloads: ids allowed, identity strings never. Sweep
        # every event for the fixture's name marker, the ІПН, and — the
        # broad net — ANY current patient display name in this tenant.
        audit_hits = await su.fetchval(
            "SELECT count(*) FROM audit.events "
            "WHERE payload_jcs::text ILIKE $1 OR payload_jcs::text LIKE $2",
            f"%{MARK}%",
            f"%{SYNTHETIC_IPN}%",
        )
        assert audit_hits == 0, (
            "REAL LEAK: patient identity found in audit payloads — the "
            "payload convention (ids only) is broken; STOP and investigate"
        )

        name_hits = await su.fetchval(
            """
            SELECT count(*)
            FROM audit.events a
            JOIN patients p ON p.tenant_id = a.tenant_id
            WHERE p.name_uk <> '' AND p.name_uk <> 'ERASED'
              AND length(p.name_uk) > 6
              AND a.payload_jcs::text ILIKE '%' || p.name_uk || '%'
            """
        )
        assert name_hits == 0, (
            "REAL LEAK: a patient display name appears in an audit payload"
        )
    finally:
        await cleanup_fixture(su)
        await su.close()
