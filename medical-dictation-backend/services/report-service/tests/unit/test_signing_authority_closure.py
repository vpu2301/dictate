"""HOTFIX — the report-service half of the signing-authority closure.

Two layers proven here:

  * the ROUTES — `POST /v1/reports/{id}/sign` and `/amend` gated on
    `report.sign` / `report.amend` rather than `report.write`, so every
    non-clinician role gets 403 and the delegation to signing-service is
    never reached;
  * the STATE MACHINE — `mark_signed` / `mark_amended` refuse a
    non-clinician principal at the transition boundary, so a route added
    later cannot bypass the route guard by driving the machine directly.

The defect these close: both routes gated on `report.write`, which
`nurse` holds. A nurse could sign a clinical report and amend a signed
one.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from auth import Claims
from report_models import ReportStatus

REQUESTER_SUB = UUID("11111111-1111-1111-1111-111111111111")
REPORT_ID = UUID("33333333-3333-3333-3333-333333333333")
VERSION_ID = UUID("55555555-5555-5555-5555-555555555555")

NON_SIGNING_ROLES = ["nurse", "tenant_admin", "auditor", "service", "knowledge_admin"]


def _claims(*roles: str) -> Claims:
    return Claims(
        sub=REQUESTER_SUB,
        tid=UUID("22222222-2222-2222-2222-222222222222"),
        roles=list(roles),
        sid="s",
        iss="https://test/issuer",
        aud="mdx",
        exp=9_999_999_999,
        iat=1_700_000_000,
        preferred_username="staff@tenant-a.example",
        name="Тест Тестовий",
    )


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TESTING", "true")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")

    from report_service import deps
    from report_service.main import create_app
    from report_service.routers import reports_amend, reports_sign

    delegated: list[dict] = []

    async def _write_event(**kwargs):
        pass

    deps.install_state(  # type: ignore[arg-type]
        SimpleNamespace(
            app_pool=object(),
            audit_writer=SimpleNamespace(write_event=_write_event),
            redis=SimpleNamespace(xadd=lambda *a, **k: None),
        )
    )

    @contextlib.asynccontextmanager
    async def _fake_tenant_conn(pool, tenant_id):
        yield None

    for mod in (reports_sign, reports_amend):
        monkeypatch.setattr(mod, "tenant_connection", _fake_tenant_conn)

    # The spy: if a refused caller ever reaches here, the guard failed.
    async def _post_signing(path, body, *, auth_header, expected):
        delegated.append({"path": path, "body": body})
        return {"envelope_id": str(uuid4()), "session_id": str(uuid4())}

    monkeypatch.setattr(reports_sign, "_post_signing", _post_signing)

    # No report row. A caller who gets past the guard therefore lands on a
    # clean 404 instead of an AttributeError, which keeps "was I refused?"
    # a readable assertion rather than an exception type.
    async def _no_report(conn, *, report_id):
        return None

    monkeypatch.setattr(reports_sign.repo, "lock_report_for_update", _no_report)
    monkeypatch.setattr(reports_amend.repo, "lock_report_for_update", _no_report)

    app = create_app()
    state = SimpleNamespace(roles=["clinician"])
    app.dependency_overrides[deps.current_user] = lambda: _claims(*state.roles)

    return SimpleNamespace(
        client=TestClient(app), delegated=delegated, state=state
    )


def _as(harness, *roles: str) -> TestClient:
    harness.state.roles = list(roles)
    return harness.client


# ── Routes: every non-clinician role is refused ────────────────────────


@pytest.mark.parametrize("role", NON_SIGNING_ROLES)
def test_sign_route_is_403_for_non_clinicians(harness, role: str) -> None:
    resp = _as(harness, role).post(
        f"/v1/reports/{REPORT_ID}/sign", json={"provider": "dev_password"}
    )
    assert resp.status_code == 403, f"{role} reached the sign route"
    assert not harness.delegated, f"{role} reached signing-service"


@pytest.mark.parametrize("role", NON_SIGNING_ROLES)
def test_amend_route_is_403_for_non_clinicians(harness, role: str) -> None:
    resp = _as(harness, role).post(
        f"/v1/reports/{REPORT_ID}/amend",
        json={"amendment_type": "addendum", "amendment_reason": "correction"},
    )
    assert resp.status_code == 403, f"{role} reached the amend route"


def test_nurse_keeps_report_write_but_not_report_sign(harness) -> None:
    """The precise shape of the fix: the nurse's authoring capability is
    untouched — only the signature is withheld. A fix that took away
    `report.write` would have broken the ward."""
    from auth.perms import can

    assert can("nurse", "report.write", "report") is True
    assert can("nurse", "report.sign", "report") is False

    resp = _as(harness, "nurse").post(
        f"/v1/reports/{REPORT_ID}/sign", json={"provider": "dev_password"}
    )
    assert resp.status_code == 403


def test_clinician_passes_the_guard(harness) -> None:
    """The door still opens. A 403 here would mean the fix broke signing
    for the one role that must have it — the response past the guard is
    a 404 only because this harness stubs no report row."""
    resp = _as(harness, "clinician").post(
        f"/v1/reports/{REPORT_ID}/sign", json={"provider": "dev_password"}
    )
    assert resp.status_code == 404  # past the guard, no such report here


# ── State machine: the transition boundary ─────────────────────────────


class _RecordingConn:
    """Fails the test loudly if a refused transition still touched the DB."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    async def fetchrow(self, sql: str, *args):
        self.statements.append(sql)
        return {"id": REPORT_ID, "status": ReportStatus.SIGNED.value}


@pytest.mark.parametrize("role", NON_SIGNING_ROLES)
async def test_sign_transition_raises_for_non_clinicians(role: str) -> None:
    from report_service.domain.report_lifecycle import (
        ReportStateMachine,
        SigningAuthorityError,
    )

    conn = _RecordingConn()
    sm = ReportStateMachine()
    with pytest.raises(SigningAuthorityError) as excinfo:
        await sm.mark_signed(conn, report_id=REPORT_ID, signer=_claims(role))
    assert role in excinfo.value.roles
    # Refused BEFORE the UPDATE — a transition that half-happened and then
    # complained would leave the report in an unexplainable state.
    assert conn.statements == []


@pytest.mark.parametrize("role", NON_SIGNING_ROLES)
async def test_amend_transition_raises_for_non_clinicians(role: str) -> None:
    from report_service.domain.report_lifecycle import (
        ReportStateMachine,
        SigningAuthorityError,
    )

    conn = _RecordingConn()
    with pytest.raises(SigningAuthorityError):
        await ReportStateMachine().mark_amended(
            conn, report_id=REPORT_ID, signer=_claims(role)
        )
    assert conn.statements == []


async def test_sign_transition_proceeds_for_a_clinician() -> None:
    from report_service.domain.report_lifecycle import (
        ReportStateMachine,
        TransitionAction,
    )

    class _OkConn:
        def __init__(self) -> None:
            self.statements: list[str] = []

        async def fetchrow(self, sql: str, *args):
            self.statements.append(sql)
            return {"id": REPORT_ID}

    conn = _OkConn()
    result = await ReportStateMachine().mark_signed(
        conn, report_id=REPORT_ID, signer=_claims("clinician")
    )
    assert result.action is TransitionAction.SIGN
    assert result.to_status is ReportStatus.SIGNED
    assert conn.statements, "the UPDATE did not run for an authorised signer"


async def test_dual_role_admin_clinician_may_drive_the_transition() -> None:
    from report_service.domain.report_lifecycle import (
        TransitionAction,
        assert_may_drive,
    )

    assert_may_drive(TransitionAction.SIGN, _claims("tenant_admin", "clinician"))
    assert_may_drive(TransitionAction.AMEND, _claims("tenant_admin", "clinician"))


def test_non_signing_transitions_are_unaffected() -> None:
    """finalize / cancel / revert carry no signing authority and must not
    have been swept up by the fix — a nurse still finalizes."""
    from report_service.domain.report_lifecycle import (
        TransitionAction,
        assert_may_drive,
    )

    for action in (
        TransitionAction.FINALIZE,
        TransitionAction.CANCEL,
        TransitionAction.REVERT_TO_DRAFT,
    ):
        assert_may_drive(action, _claims("nurse"))  # no raise
