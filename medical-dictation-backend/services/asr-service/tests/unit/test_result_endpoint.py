"""Behavioural tests for ``GET /asr/jobs/{id}/result`` (spec §2.5).

The result endpoint hands back a short-TTL pre-signed URL only when the job is
COMPLETE; otherwise it returns an RFC 9457 409. We exercise the real handler
with the auth dependency overridden and the DB/store boundary stubbed, so no
infra is required.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from asr_models import JobStatus, TranscriptionJobView
from auth import Claims


def _clinician_claims() -> Claims:
    return Claims(
        sub=uuid4(),
        tid=uuid4(),
        roles=["clinician"],
        sid="test-session",
        iss="https://test/issuer",
        aud="mdx",
        exp=9_999_999_999,
        iat=1_700_000_000,
    )


def _job_view(status: JobStatus) -> TranscriptionJobView:
    return TranscriptionJobView(
        id=uuid4(),
        tenant_id=uuid4(),
        audio_id=uuid4(),
        requester_sub=uuid4(),
        prompt_id=uuid4(),
        language="uk",
        model="large-v3",
        status=status,
        queued_at="2026-05-20T00:00:00Z",
    )


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("TESTING", "true")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")

    from asr_service import deps
    from asr_service.main import create_app
    from asr_service.routers import jobs

    # Stubbed service state: only the bits the handler touches.
    async def _presigned(*, key: str, expires_in: int) -> str:
        return f"https://minio.test/{key}?ttl={expires_in}"

    fake_state = SimpleNamespace(
        app_pool=object(),
        transcript_store=SimpleNamespace(presigned_url=_presigned),
    )
    deps.install_state(fake_state)  # type: ignore[arg-type]

    @contextlib.asynccontextmanager
    async def _fake_tenant_conn(pool, tenant_id):  # noqa: ANN001
        yield None

    monkeypatch.setattr(jobs, "tenant_connection", _fake_tenant_conn)

    app = create_app()
    app.dependency_overrides[deps.current_user] = _clinician_claims
    return TestClient(app)


def test_result_409_when_not_complete(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from asr_service.routers import jobs

    async def _get_job(conn, *, job_id):  # noqa: ANN001
        return _job_view(JobStatus.RUNNING)

    monkeypatch.setattr(jobs.repository, "get_job", _get_job)

    resp = client.get(f"/asr/jobs/{uuid4()}/result")
    assert resp.status_code == 409
    # The shared handler renders RFC 9457 problem+json; the dict detail is
    # surfaced in the body (matching the POST validation/rate-limit siblings).
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert "urn:mdx:asr:result:not-ready" in resp.text
    assert "running" in resp.text


def test_result_404_when_missing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from asr_service.routers import jobs

    async def _get_job(conn, *, job_id):  # noqa: ANN001
        return None

    monkeypatch.setattr(jobs.repository, "get_job", _get_job)

    resp = client.get(f"/asr/jobs/{uuid4()}/result")
    assert resp.status_code == 404


def test_result_200_with_presigned_url_when_complete(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from asr_service.routers import jobs

    async def _get_job(conn, *, job_id):  # noqa: ANN001
        return _job_view(JobStatus.COMPLETE)

    monkeypatch.setattr(jobs.repository, "get_job", _get_job)

    job_id = uuid4()
    resp = client.get(f"/asr/jobs/{job_id}/result")
    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"] == str(job_id)
    assert body["presigned_url"].startswith("https://minio.test/")
    assert body["expires_in"] == 300  # default S3_PRESIGNED_TTL_SECONDS (5 min)
