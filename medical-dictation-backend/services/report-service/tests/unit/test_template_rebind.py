"""Sprint-17 re-bind surface: bound-reports listing + rebind endpoint.

Real handlers, auth overridden, repository monkeypatched (router tests);
plus a scripted fake connection driving the repository decision tree.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from auth import Claims
from report_service.domain import repository as repo_mod


def _admin_claims() -> Claims:
    return Claims(
        sub=uuid4(),
        tid=uuid4(),
        roles=["tenant_admin"],
        sid="test-session",
        iss="https://test/issuer",
        aud="mdx",
        exp=9_999_999_999,
        iat=1_700_000_000,
    )


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("TESTING", "true")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")

    from report_service import deps
    from report_service.main import create_app
    from report_service.routers import templates

    audit_calls: list[dict] = []

    async def _write_event(**kwargs):  # noqa: ANN003
        audit_calls.append(kwargs)

    deps.install_state(  # type: ignore[arg-type]
        SimpleNamespace(
            app_pool=object(),
            audit_writer=SimpleNamespace(write_event=_write_event),
            template_cache=SimpleNamespace(),
        )
    )

    @contextlib.asynccontextmanager
    async def _fake_tenant_conn(pool, tenant_id):  # noqa: ANN001
        yield None

    monkeypatch.setattr(templates, "tenant_connection", _fake_tenant_conn)

    app = create_app()
    app.dependency_overrides[deps.current_user] = _admin_claims
    c = TestClient(app)
    c.audit_calls = audit_calls  # type: ignore[attr-defined]
    return c


# ── GET /templates/{id}/bound-reports ───────────────────────────────


def test_bound_reports_lists_phi_free_rows(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from report_service.routers import templates

    template_id = uuid4()
    report_id = uuid4()
    now = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)

    async def _get_template(conn, *, template_id):  # noqa: ANN001
        return {"id": template_id}

    async def _list(conn, *, template_id, limit):  # noqa: ANN001
        assert limit == 50
        return [
            {
                "id": report_id,
                "status": "draft",
                "created_at": now,
                "updated_at": now,
            }
        ]

    monkeypatch.setattr(templates.repository, "get_template", _get_template)
    monkeypatch.setattr(templates.repository, "list_bound_reports", _list)

    resp = client.get(f"/templates/{template_id}/bound-reports")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    # PHI-free contract: exactly these keys, nothing clinical.
    assert set(rows[0].keys()) == {"report_id", "status", "created_at", "updated_at"}
    assert rows[0]["report_id"] == str(report_id)
    assert rows[0]["status"] == "draft"


def test_bound_reports_404_when_template_invisible(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from report_service.routers import templates

    async def _get_template(conn, *, template_id):  # noqa: ANN001
        return None

    monkeypatch.setattr(templates.repository, "get_template", _get_template)
    assert client.get(f"/templates/{uuid4()}/bound-reports").status_code == 404


# ── POST /templates/{id}/rebind — router mapping ────────────────────


def _rebind(client: TestClient, template_id: UUID, **body_overrides):  # noqa: ANN003
    body = {
        "report_id": str(body_overrides.pop("report_id", uuid4())),
        "to_template_id": str(body_overrides.pop("to_template_id", uuid4())),
    }
    body.update(body_overrides)
    return client.post(f"/templates/{template_id}/rebind", json=body)


def test_rebind_happy_path_audits(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from report_service.routers import templates

    template_id = uuid4()
    report_id = uuid4()
    to_template_id = uuid4()

    async def _rebind_repo(conn, *, template_id, report_id, to_template_id):  # noqa: ANN001
        return "ok"

    monkeypatch.setattr(templates.repository, "rebind_report", _rebind_repo)

    resp = _rebind(
        client, template_id, report_id=report_id, to_template_id=to_template_id
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "report_id": str(report_id),
        "from_template_id": str(template_id),
        "to_template_id": str(to_template_id),
    }

    calls = client.audit_calls  # type: ignore[attr-defined]
    assert len(calls) == 1
    assert calls[0]["kind"] == "template.rebound"
    assert calls[0]["target_id"] == str(template_id)
    assert calls[0]["payload"] == {
        "report_id": str(report_id),
        "from_template_id": str(template_id),
        "to_template_id": str(to_template_id),
    }


@pytest.mark.parametrize(
    ("outcome", "expected_status", "detail_fragment"),
    [
        ("template_not_found", 404, "template not found"),
        ("report_not_found", 404, "report not found"),
        ("not_bound", 409, "not bound to this template"),
        ("not_draft", 409, "only draft reports"),
        ("same_template", 409, "already bound"),
        ("target_not_found", 404, "target template not visible"),
        ("target_deprecated", 409, "target template is deprecated"),
        ("language_mismatch", 409, "language differs"),
    ],
)
def test_rebind_guard_mapping(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    expected_status: int,
    detail_fragment: str,
) -> None:
    from report_service.routers import templates

    async def _rebind_repo(conn, **kwargs):  # noqa: ANN001, ANN003
        return outcome

    monkeypatch.setattr(templates.repository, "rebind_report", _rebind_repo)

    resp = _rebind(client, uuid4())
    assert resp.status_code == expected_status
    assert detail_fragment in resp.json()["detail"]
    assert client.audit_calls == []  # type: ignore[attr-defined]


def test_rebind_rejects_extra_field(client: TestClient) -> None:
    resp = client.post(
        f"/templates/{uuid4()}/rebind",
        json={
            "report_id": str(uuid4()),
            "to_template_id": str(uuid4()),
            "bogus": "x",
        },
    )
    assert resp.status_code == 422


# ── repository.rebind_report decision tree (scripted fake conn) ─────


class _FakeConn:
    """Scripted connection: answers fetchrow by matching query fragments."""

    def __init__(
        self,
        *,
        source_template: dict | None,
        report: dict | None,
        target_template: dict | None,
    ) -> None:
        self._source = source_template
        self._report = report
        self._target = target_template
        self.executed: list[tuple[str, tuple]] = []

    async def fetchrow(self, query: str, *args):  # noqa: ANN001, ANN002
        if "FROM reports" in query:
            return self._report
        assert "FROM templates" in query
        # First templates read is the source (path id), second the target.
        if self._source is not None and args[0] == self._source["id"]:
            return self._source
        if self._target is not None and args[0] == self._target["id"]:
            return self._target
        return None

    async def execute(self, query: str, *args):  # noqa: ANN001, ANN002
        self.executed.append((query, args))


_SRC_ID = uuid4()
_TGT_ID = uuid4()
_REP_ID = uuid4()


def _conn(
    *,
    report_status: str = "draft",
    report_template: UUID = _SRC_ID,
    target_status: str = "active",
    target_language: str = "uk",
    report_exists: bool = True,
    target_exists: bool = True,
    source_exists: bool = True,
) -> _FakeConn:
    return _FakeConn(
        source_template=(
            {"id": _SRC_ID, "language": "uk"} if source_exists else None
        ),
        report=(
            {"id": _REP_ID, "status": report_status, "template_id": report_template}
            if report_exists
            else None
        ),
        target_template=(
            {
                "id": _TGT_ID,
                "status": target_status,
                "language": target_language,
                "schema_version": 3,
            }
            if target_exists
            else None
        ),
    )


async def _run(conn: _FakeConn, to_template_id: UUID = _TGT_ID) -> str:
    return await repo_mod.rebind_report(
        conn,  # type: ignore[arg-type]
        template_id=_SRC_ID,
        report_id=_REP_ID,
        to_template_id=to_template_id,
    )


@pytest.mark.asyncio
async def test_repo_rebind_ok_updates_row() -> None:
    conn = _conn()
    assert await _run(conn) == "ok"
    assert len(conn.executed) == 1
    query, args = conn.executed[0]
    assert "SET template_id" in query
    assert args == (_REP_ID, _TGT_ID, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("conn_kwargs", "expected"),
    [
        ({"source_exists": False}, "template_not_found"),
        ({"report_exists": False}, "report_not_found"),
        ({"report_template": uuid4()}, "not_bound"),
        ({"report_status": "finalized"}, "not_draft"),
        ({"report_status": "signed"}, "not_draft"),
        ({"report_status": "cancelled"}, "not_draft"),
        ({"target_exists": False}, "target_not_found"),
        ({"target_status": "deprecated"}, "target_deprecated"),
        ({"target_language": "en"}, "language_mismatch"),
    ],
)
async def test_repo_rebind_guards(conn_kwargs: dict, expected: str) -> None:
    conn = _conn(**conn_kwargs)
    assert await _run(conn) == expected
    assert conn.executed == []


@pytest.mark.asyncio
async def test_repo_rebind_same_template_short_circuits() -> None:
    conn = _conn()
    assert await _run(conn, to_template_id=_SRC_ID) == "same_template"
    assert conn.executed == []
