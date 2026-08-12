"""Which failures are terminal, which get another go, and who writes the row.

The rule these tests hold: when the worker stops working on a job, either
the job row says why, or the message is still on the queue. Never neither.
That "neither" is exactly what used to happen — a retryable failure that
exhausted its retries left the DLQ holding the message and the row holding
``running``, forever.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from asr_models import JobEnqueuePayload, JobErrorKind
from asr_worker import processor
from asr_worker.processor import (
    _classified,
    _fail_or_retry,
    _NonRetryableError,
    _RetryableError,
)
from messaging import Message


@pytest.mark.parametrize(
    "kind,expected",
    [
        (JobErrorKind.CORRUPT_AUDIO, _NonRetryableError),
        (JobErrorKind.NO_SPEECH, _NonRetryableError),
        (JobErrorKind.AUDIO_MISSING, _NonRetryableError),
        (JobErrorKind.DECRYPT_FAILED, _NonRetryableError),
        (JobErrorKind.GPU_OOM, _NonRetryableError),
        (JobErrorKind.TIMEOUT, _NonRetryableError),
        (JobErrorKind.BAD_PAYLOAD, _NonRetryableError),
        (JobErrorKind.STORAGE_UNAVAILABLE, _RetryableError),
        (JobErrorKind.MODEL_UNAVAILABLE, _RetryableError),
        (JobErrorKind.RESULT_STORE_FAILED, _RetryableError),
        (JobErrorKind.DB_UNAVAILABLE, _RetryableError),
    ],
)
def test_retry_policy_comes_from_the_vocabulary(
    kind: JobErrorKind, expected: type[Exception]
) -> None:
    err = _classified(kind, "detail")
    assert isinstance(err, expected)
    assert err.kind == str(kind)


def _message(payload: JobEnqueuePayload | None) -> Message:
    value = payload.model_dump_json().encode("utf-8") if payload else b"{not json"
    return Message(
        topic="mdx.asr.jobs",
        key=b"k",
        value=value,
        headers={"_id": "1-0"},
        timestamp_ms=0,
    )


def _payload() -> JobEnqueuePayload:
    return JobEnqueuePayload(
        job_id=uuid4(),
        tenant_id=uuid4(),
        audio_id=uuid4(),
        prompt_id=uuid4(),
        language="uk",
        requester_sub=uuid4(),
    )


class _Consumer:
    """A consumer whose ``fail`` reports whether it dead-lettered."""

    def __init__(self, *, dead_letters: bool) -> None:
        self.dead_letters = dead_letters
        self.calls: list[str] = []

    async def fail(self, message: Message, *, error_kind: str) -> bool:
        self.calls.append(error_kind)
        return self.dead_letters


@pytest.fixture
def marked(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture ``_mark_failed`` instead of touching a database."""
    recorded: list[dict[str, Any]] = []

    async def fake_mark_failed(
        state: Any, tenant_id: Any, job_id: Any, *, kind: str, detail: str, **_: Any
    ) -> None:
        recorded.append({"job_id": job_id, "kind": kind, "detail": detail})

    monkeypatch.setattr(processor, "_mark_failed", fake_mark_failed)
    return recorded


async def test_retry_leaves_the_row_alone(marked: list[dict[str, Any]]) -> None:
    # Still retrying is not a failure of the job — it is a failure of one
    # attempt. The row stays `running` and the message comes back.
    consumer = _Consumer(dead_letters=False)
    err = _RetryableError(str(JobErrorKind.STORAGE_UNAVAILABLE), "minio down")

    await _fail_or_retry(object(), consumer, _message(_payload()), err)  # type: ignore[arg-type]

    assert consumer.calls == [str(JobErrorKind.STORAGE_UNAVAILABLE)]
    assert marked == []


async def test_dead_letter_closes_the_job_out(marked: list[dict[str, Any]]) -> None:
    payload = _payload()
    consumer = _Consumer(dead_letters=True)
    err = _RetryableError(str(JobErrorKind.STORAGE_UNAVAILABLE), "minio down")

    await _fail_or_retry(object(), consumer, _message(payload), err)  # type: ignore[arg-type]

    assert len(marked) == 1
    assert marked[0]["job_id"] == payload.job_id
    assert marked[0]["kind"] == str(JobErrorKind.RETRY_EXHAUSTED)
    # The kind that kept failing is preserved in the detail — without it an
    # operator reading the row knows only that it gave up, not on what.
    assert str(JobErrorKind.STORAGE_UNAVAILABLE) in marked[0]["detail"]


async def test_dead_letter_of_an_unreadable_payload_marks_nothing(
    marked: list[dict[str, Any]],
) -> None:
    # There is no job id to fail. The DLQ entry is the whole record, which
    # is why bad_payload is never retried in the first place.
    consumer = _Consumer(dead_letters=True)
    err = _RetryableError(str(JobErrorKind.UNHANDLED), "boom")

    await _fail_or_retry(object(), consumer, _message(None), err)  # type: ignore[arg-type]

    assert marked == []
