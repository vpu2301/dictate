"""Behavioural tests for ``GET /asr/jobs/{id}/result`` (spec §2.5).

The result endpoint decrypts the stored transcript through
``EncryptedObjectStore.get()``, runs it through nlp-service's batch
pipeline (dictated punctuation, number normalization), and returns a
``TranscriptResultView`` (ADR-0011 forbids client-side decrypt; presigned
URLs only ever serve ciphertext). NLP failures degrade to the raw
transcript. Not-ready is an explicit 409; an erased ciphertext is 410.
We exercise the real handler with the auth dependency overridden and the
DB/store/NLP boundaries stubbed, so no infra is required.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from typing import Any
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
                text="скарги на кашель крапка",
                start_ms=0,
                end_ms=2600,
                words=[
                    WordTiming(text="скарги", start_ms=0, end_ms=700, probability=0.97),
                    WordTiming(text="на", start_ms=700, end_ms=800, probability=0.99),
                    WordTiming(text="кашель", start_ms=800, end_ms=1500, probability=0.88),
                    WordTiming(text="крапка", start_ms=2000, end_ms=2600, probability=0.96),
                ],
                avg_confidence=0.95,
            ),
            Segment(
                text="крапка",
                start_ms=3000,
                end_ms=3500,
                words=[WordTiming(text="крапка", start_ms=3000, end_ms=3500, probability=0.97)],
                avg_confidence=0.97,
            ),
        ],
        metadata=TranscriptionMetadata(
            model="large-v3",
            vad_seconds_speech=3.5,
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


class _FakeNlpClient:
    """Mimics the enriched batch response for the two-segment fixture."""

    def __init__(self) -> None:
        self.response: dict[str, Any] | None = {
            "pipeline_version": "nlp-v1.0.0",
            "segments": [
                {
                    "text": "Скарги на кашель.",
                    "confidence_spans": [
                        {"start_char": 10, "end_char": 16, "level": "moderate"}
                    ],
                },
                {"text": ".", "confidence_spans": []},
            ],
        }
        self.calls: list[dict[str, Any]] = []

    async def process_segments(self, **kwargs: Any) -> dict[str, Any] | None:
        self.calls.append(kwargs)
        return self.response


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
    nlp = _FakeNlpClient()
    fake_state = SimpleNamespace(
        app_pool=object(),
        transcript_store=store,
        audit_writer=audit,
        nlp_client=nlp,
    )
    deps.install_state(fake_state)  # type: ignore[arg-type]

    @contextlib.asynccontextmanager
    async def _fake_tenant_conn(pool, tenant_id):  # noqa: ANN001
        yield None

    monkeypatch.setattr(jobs, "tenant_connection", _fake_tenant_conn)

    app = create_app()
    app.dependency_overrides[deps.current_user] = _clinician_claims
    return SimpleNamespace(client=TestClient(app), store=store, audit=audit, nlp=nlp)


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


def test_result_409_on_a_failed_job_carries_the_failure_vocabulary(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client polling for a transcript should learn in one response that
    it is not coming, and whether resubmitting would help — not keep
    polling a job that failed hours ago."""
    from asr_service.routers import jobs

    view = _job_view(JobStatus.FAILED).model_copy(update={"error_kind": "corrupt_audio"})

    async def _get_job(conn, *, job_id):  # noqa: ANN001
        return view

    monkeypatch.setattr(jobs.repository, "get_job", _get_job)

    resp = rig.client.get(f"/asr/jobs/{uuid4()}/result")
    assert resp.status_code == 409
    body = resp.json()
    assert body["job_status"] == "failed"
    assert body["error_kind"] == "corrupt_audio"
    assert body["error_stage"] == "decode"
    assert body["error_retryable"] is False
    assert "decoded" in body["error_message"]


def test_result_404_when_missing(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from asr_service.routers import jobs

    async def _get_job(conn, *, job_id):  # noqa: ANN001
        return None

    monkeypatch.setattr(jobs.repository, "get_job", _get_job)

    resp = rig.client.get(f"/asr/jobs/{uuid4()}/result")
    assert resp.status_code == 404


def test_result_200_nlp_enriched(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from asr_service.routers import jobs

    view = _job_view(JobStatus.COMPLETE)

    async def _get_job(conn, *, job_id):  # noqa: ANN001
        return view

    monkeypatch.setattr(jobs.repository, "get_job", _get_job)

    job_id = uuid4()
    resp = rig.client.get(
        f"/asr/jobs/{job_id}/result", headers={"Authorization": "Bearer user-token"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"] == str(job_id)
    assert body["nlp_applied"] is True
    assert body["nlp_pipeline_version"] == "nlp-v1.0.0"

    # The standalone «крапка» segment (NLP → ".") merged into the previous
    # one without doubling the period; its end_ms extends the merged segment.
    assert len(body["segments"]) == 1
    seg = body["segments"][0]
    assert seg["text"] == "Скарги на кашель."
    assert seg["raw_text"] == "скарги на кашель крапка"
    assert seg["end_ms"] == 3500
    assert seg["confidence_spans"] == [
        {"start_char": 10, "end_char": 16, "level": "moderate"}
    ]
    assert seg["words"][0]["text"] == "скарги"

    # The caller's bearer was forwarded verbatim to nlp-service, with
    # words converted to seconds.
    (call,) = rig.nlp.calls
    assert call["authorization"] == "Bearer user-token"
    assert call["language"] == "uk"
    assert call["segments"][0]["words"][0]["start_s"] == 0.0
    assert call["segments"][0]["words"][3]["probability"] == 0.96

    # Decrypt went through the envelope path with the job's key + AAD.
    (store_call,) = rig.store.calls
    assert store_call["key"] == f"{_TENANT}/{job_id}.json.enc"
    assert store_call["aad"] == job_id.bytes

    # Plaintext PHI reads are audited.
    (event,) = rig.audit.events
    assert event["kind"] == "asr.transcript_accessed"
    assert event["target_id"] == str(job_id)


def test_result_200_raw_fallback_when_nlp_down(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from asr_service.routers import jobs

    async def _get_job(conn, *, job_id):  # noqa: ANN001
        return _job_view(JobStatus.COMPLETE)

    monkeypatch.setattr(jobs.repository, "get_job", _get_job)
    rig.nlp.response = None  # nlp-service unreachable

    resp = rig.client.get(f"/asr/jobs/{uuid4()}/result")
    assert resp.status_code == 200
    body = resp.json()
    assert body["nlp_applied"] is False
    assert body["nlp_pipeline_version"] is None
    assert len(body["segments"]) == 2
    assert body["segments"][0]["text"] == "скарги на кашель крапка"  # raw, untouched
    assert body["segments"][0]["text"] == body["segments"][0]["raw_text"]


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
