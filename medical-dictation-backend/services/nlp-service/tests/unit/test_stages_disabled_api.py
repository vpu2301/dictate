"""Wire-level validation for ``stages_disabled`` (sprint 14).

The field is additive on ``ProcessRequest``/``BatchProcessRequest``
(``extra='forbid'``): conversation mode passes ``["voice_commands"]``
so patient speech can never trigger editing operations. Unknown stage
names must be rejected at the model boundary.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nlp_service.api.process import BatchProcessRequest, ProcessRequest


def test_process_request_accepts_voice_commands_disabled() -> None:
    req = ProcessRequest(
        text="привіт", language="uk", stages_disabled=["voice_commands"]
    )
    assert req.stages_disabled == ["voice_commands"]


def test_process_request_defaults_to_no_disabled_stages() -> None:
    req = ProcessRequest(text="привіт", language="uk")
    assert req.stages_disabled == []


def test_process_request_accepts_all_known_stage_names() -> None:
    names = [
        "voice_commands",
        "punctuation",
        "number_norm",
        "date_norm",
        "abbreviation",
        "field_extraction",
        "confidence",
    ]
    req = ProcessRequest(text="привіт", language="uk", stages_disabled=names)
    assert req.stages_disabled == names


def test_process_request_rejects_unknown_stage_name() -> None:
    with pytest.raises(ValidationError):
        ProcessRequest(text="привіт", language="uk", stages_disabled=["not_a_stage"])


def test_batch_request_accepts_and_rejects() -> None:
    req = BatchProcessRequest(
        segments=[], language="uk", stages_disabled=["voice_commands"]
    )
    assert req.stages_disabled == ["voice_commands"]
    with pytest.raises(ValidationError):
        BatchProcessRequest(segments=[], language="uk", stages_disabled=["bogus"])
