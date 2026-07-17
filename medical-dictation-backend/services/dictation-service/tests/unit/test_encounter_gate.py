"""S11 step 02 — encounter-linkage gate matrix (pure logic; no DB).

The DB-backed half (RLS invisibility, FK, RESTRICT) is proven in
``services/asr-service/tests/integration/test_encounter_fk_db.py`` and the
live WS battery; here we pin the status→rejection mapping and the
recoverability contract of the two new protocol codes.
"""

from __future__ import annotations

import pytest

from dictation_service.domain.encounters import encounter_gate
from dictation_service.protocol.error_catalogue import ErrorCode, is_recoverable


def test_nonexistent_or_cross_tenant_is_invalid() -> None:
    # RLS makes a cross-tenant encounter fetch None — identical to
    # nonexistent, so there is exactly one code and no existence oracle.
    assert encounter_gate(None) is ErrorCode.ENCOUNTER_INVALID


def test_cancelled_is_closed() -> None:
    assert encounter_gate("cancelled") is ErrorCode.ENCOUNTER_CLOSED


@pytest.mark.parametrize("status", ["scheduled", "in_progress", "completed"])
def test_dictable_statuses_pass(status: str) -> None:
    # `completed` is the as-built default (0031) — encounters are routinely
    # recorded post-visit, so it MUST remain dictable.
    assert encounter_gate(status) is None


def test_both_codes_are_terminal() -> None:
    assert not is_recoverable(ErrorCode.ENCOUNTER_INVALID)
    assert not is_recoverable(ErrorCode.ENCOUNTER_CLOSED)
