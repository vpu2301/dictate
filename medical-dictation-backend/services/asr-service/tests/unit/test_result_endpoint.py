"""Behavioural tests for ``GET /asr/jobs/{id}/result`` (spec §2.5).

The result endpoint decrypts the stored transcript through
``EncryptedObjectStore.get()`` and returns the plaintext
``TranscriptionOutput`` (ADR-0011 forbids client-side decrypt; presigned
URLs only ever serve ciphertext). Not-ready is an explicit 409; an erased
ciphertext is 410. We exercise the real handler with the auth dependency
overridden and the DB/store boundary stubbed, so no infra is required.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from asr_models import (
    JobStatus,
    Segment,
    TranscriptionJobView,
    TranscriptionMetadata,
    TranscriptionOutput,
    WordTiming,
)
from auth import Claims
from storage import ObjectNotFoundError

_TENANT = uuid4()


def _clinician_claims() -> Claims:
    return Claims(
        sub=uuid4(),
        tid=_TENANT,
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
        tenant_id=_TENANT,
        audio_id=uuid4(),
        requester_sub=uuid4(),
        prompt_id=uuid4(),
        language="uk",
        model="large-v3",
        status=status,
        queued_at="2026-05-20T00:00:00Z",
    )


def _output() -> TranscriptionOutput:
    return TranscriptionOutput(
        language="uk",
        segments=[
            Segment(
                text="пацієнт скаржиться",
                start_ms=0,
                end_ms=1800,
                words=[
                    WordTiming(text="пацієнт", start_ms=0, end_ms=900, probability=0.97),
                    WordTiming(text="скаржиться", start_ms=900, end_ms=1800, probability=0.88),
                ],
                avg_confidence=0.92,
            )
        ],
        metadata=TranscriptionMetadata(
            model="large-v3",
            vad_seconds_speech=1.8,
            infer_seconds=0.4,
            beam_size=5,
        ),
    )


class _FakeTranscriptStore:
    def __init__(self) -> None:
        self.body: bytes | None = _output().model_dump_json().encode("utf-8")
        self.calls: list[dict[str, object]] = []

    async def get(self, *, key: str, tenant_id: UUID, aad: bytes | None = None) -> bytes:
        self.calls.append({"key": key, "tenant_id": tenant_id, "aad": aad})
        if self.body is None:
            raise ObjectNotFoundError(bucket="mdx-transcripts", key=key)
        return self.body


class _FakeAuditWriter:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    async def write_event(self, **kwargs: object) -> None:
        self.events.append(kwargs)


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    monkeypatch.setenv("TESTING", "true")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")

    from asr_service import deps
    from asr_service.main import create_app
    from asr_service.routers import jobs

    store = _FakeTranscriptStore()
    audit = _FakeAuditWriter()
    fake_state = SimpleNamespace(
        app_pool=object(),
        transcript_store=store,
        audit_writer=audit,
    )
    deps.install_state(fake_state)  # type: ignore[arg-type]

    @contextlib.asynccontextmanager
    async def _fake_tenant_conn(pool, tenant_id):  # noqa: ANN001
        yield None

    monkeypatch.setattr(jobs, "tenant_connection", _fake_tenant_conn)

    app = create_app()
    app.dependency_overrides[deps.current_user] = _clinician_claims
    return SimpleNamespace(client=TestClient(app), store=store, audit=audit)


def test_result_409_when_not_complete(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from asr_service.routers import jobs

    async def _get_job(conn, *, job_id):  # noqa: ANN001
        return _job_view(JobStatus.RUNNING)

    monkeypatch.setattr(jobs.repository, "get_job", _get_job)

    resp = rig.client.get(f"/asr/jobs/{uuid4()}/result")
    assert resp.status_code == 409
    # The shared handler renders RFC 9457 problem+json; the dict detail is
    # surfaced in the body (matching the POST validation/rate-limit siblings).
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert "urn:mdx:asr:result:not-ready" in resp.text
    assert "running" in resp.text


def test_result_404_when_missing(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from asr_service.routers import jobs

    async def _get_job(conn, *, job_id):  # noqa: ANN001
        return None

    monkeypatch.setattr(jobs.repository, "get_job", _get_job)

    resp = rig.client.get(f"/asr/jobs/{uuid4()}/result")
    assert resp.status_code == 404


def test_result_200_plaintext_output_when_complete(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from asr_service.routers import jobs

    view = _job_view(JobStatus.COMPLETE)

    async def _get_job(conn, *, job_id):  # noqa: ANN001
        return view

    monkeypatch.setattr(jobs.repository, "get_job", _get_job)

    job_id = uuid4()
    resp = rig.client.get(f"/asr/jobs/{job_id}/result")
    assert resp.status_code == 200
    body = resp.json()
    assert body["language"] == "uk"
    assert body["segments"][0]["text"] == "пацієнт скаржиться"
    assert body["segments"][0]["words"][0]["text"] == "пацієнт"
    assert "presigned_url" not in body

    # Decrypt went through the envelope path with the job's key + AAD.
    (call,) = rig.store.calls
    assert call["key"] == f"{_TENANT}/{job_id}.json.enc"
    assert call["tenant_id"] == _TENANT
    assert call["aad"] == job_id.bytes

    # Plaintext PHI reads are audited.
    (event,) = rig.audit.events
    assert event["kind"] == "asr.transcript_accessed"
    assert event["target_id"] == str(job_id)
    assert event["payload"]["audio_id"] == str(view.audio_id)  # type: ignore[index]


def test_result_410_when_ciphertext_erased(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from asr_service.routers import jobs

    async def _get_job(conn, *, job_id):  # noqa: ANN001
        return _job_view(JobStatus.COMPLETE)

    monkeypatch.setattr(jobs.repository, "get_job", _get_job)
    rig.store.body = None  # object deleted by retention TTL / erasure engine

    resp = rig.client.get(f"/asr/jobs/{uuid4()}/result")
    assert resp.status_code == 410
    assert "urn:mdx:asr:result:erased" in resp.text
    assert rig.audit.events == []  # nothing served → nothing audited
