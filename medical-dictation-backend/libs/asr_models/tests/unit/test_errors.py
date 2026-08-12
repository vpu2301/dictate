"""The failure vocabulary is a contract, so these tests guard its edges.

Two things are easy to break here and hard to notice: adding a
``JobErrorKind`` without a spec (the kind then decodes as "unrecognised"
on an API that is supposed to define it), and letting an exception string
leak into the user-facing message.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from asr_models import (
    ERROR_SPECS,
    JobErrorKind,
    JobStatus,
    TranscriptionJobView,
    is_retryable,
    spec_for,
)


def _view(error_kind: str | None) -> TranscriptionJobView:
    return TranscriptionJobView(
        id=uuid4(),
        tenant_id=uuid4(),
        audio_id=uuid4(),
        requester_sub=uuid4(),
        prompt_id=uuid4(),
        language="uk",
        model="large-v3",
        status=JobStatus.FAILED if error_kind else JobStatus.QUEUED,
        error_kind=error_kind,
        queued_at="2026-08-11T00:00:00Z",
    )


def test_every_kind_has_a_spec() -> None:
    missing = [k for k in JobErrorKind if str(k) not in ERROR_SPECS]
    assert not missing, f"kinds without a spec: {missing}"


@pytest.mark.parametrize("kind", list(JobErrorKind))
def test_specs_are_self_consistent(kind: JobErrorKind) -> None:
    spec = spec_for(str(kind))
    assert spec is not None
    assert spec.kind == str(kind)
    assert spec.message and spec.message[0].isupper()
    # The message is shown to a clinician and must be built from the kind
    # alone — never from the exception detail, which can quote the audio
    # (ADR-0031). Nothing that looks like a Python exception belongs here.
    assert "Error(" not in spec.message
    assert "Traceback" not in spec.message


def test_unknown_kind_decodes_without_claiming_it_is_retryable() -> None:
    # A job failed by a newer worker than this reader. Naming it retryable
    # would promise a recovery this build knows nothing about.
    spec = spec_for("kind_from_the_future")
    assert spec is not None
    assert spec.retryable is False
    assert is_retryable("kind_from_the_future") is False


def test_no_kind_means_no_derived_fields() -> None:
    view = _view(None)
    assert view.error_stage is None
    assert view.error_retryable is None
    assert view.error_message is None


def test_view_derives_stage_and_advice_from_the_kind() -> None:
    view = _view(str(JobErrorKind.GPU_OOM))
    assert view.error_stage == "inference"
    assert view.error_retryable is False
    assert "GPU" in (view.error_message or "")


def test_view_cannot_be_handed_derived_fields_that_contradict_the_kind() -> None:
    # Computed, not stored: a caller cannot ship a `corrupt_audio` job
    # labelled "transient, try again".
    view = TranscriptionJobView.model_validate(
        {
            **_view(str(JobErrorKind.CORRUPT_AUDIO)).model_dump(mode="json"),
            "error_stage": "lifecycle",
            "error_retryable": True,
            "error_message": "totally fine, retry it",
        }
    )
    assert view.error_stage == "decode"
    assert view.error_retryable is False
    assert view.error_message == spec_for(str(JobErrorKind.CORRUPT_AUDIO)).message  # type: ignore[union-attr]


def test_model_copy_cannot_desync_the_derived_fields() -> None:
    # `model_copy(update=...)` skips validators — the list endpoint uses it
    # to build the PHI-free projection. Computed fields follow the kind
    # through the copy; stored ones would have been left behind.
    copied = _view(None).model_copy(update={"error_kind": str(JobErrorKind.TIMEOUT)})
    assert copied.error_stage == "inference"
    assert copied.model_dump()["error_stage"] == "inference"
