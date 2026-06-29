"""Behavioural test for ``POST /templates`` plain create (M1·A4).

Real handler, auth overridden, DB/audit stubbed — no infra required.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from auth import Claims
from template_models import FieldType, TemplateDefinition, TemplateSection


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


def _definition() -> dict:
    section = TemplateSection(
        id="anamnesis",
        name="Anamnesis",
        voice_aliases=("анамнез",),
        required=True,
        field_type=FieldType.FREE_TEXT,
        asr_prompt="коротка історія хвороби",
        min_chars=0,
    )
    definition = TemplateDefinition(
        code="cardiology_outpatient",
        name="Cardiology outpatient",
        language="uk",
        specialty="cardiology",
        sections=(section,),
    )
    return definition.model_dump(mode="json")


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


def test_create_template_returns_id_and_audits(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from report_service.routers import templates

    new_id = uuid4()
    captured: dict = {}

    async def _create(conn, *, tenant_id, definition):  # noqa: ANN001
        captured["definition"] = definition
        return new_id

    monkeypatch.setattr(templates.repository, "create_template", _create)

    resp = client.post("/templates", json=_definition())
    assert resp.status_code == 201
    assert resp.json() == {"id": str(new_id)}
    assert captured["definition"].code == "cardiology_outpatient"

    calls = client.audit_calls  # type: ignore[attr-defined]
    assert len(calls) == 1
    assert calls[0]["kind"] == "template.created"
    assert calls[0]["payload"] == {"code": "cardiology_outpatient", "specialty": "cardiology"}


def test_create_template_rejects_extra_field(client: TestClient) -> None:
    body = _definition()
    body["bogus"] = "x"  # extra="forbid" on TemplateDefinition
    assert client.post("/templates", json=body).status_code == 422
