"""Sprint-14 persisted-transcript shape contract (``_transcript_to_jsonb``).

Two promises live here:

* **Dictation is byte-compatible with pre-sprint-14.** The segment and
  word key sets are pinned EXACTLY — a new key would silently break every
  existing consumer of ``dictation_sessions.transcript``.
* **Conversation is honest.** It adds ids + speaker proposals, and a
  ``UNKNOWN``/null speaker survives into persistence rather than being
  papered over into a party.

Pure: no DB, no models — the diarization stream and mapping inference are
stubs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from dictation_service.diarization.mapping import MappingHypothesis
from dictation_service.session.finalize import _transcript_to_jsonb
from dictation_service.session.manager import SessionContext

SEGMENT_KEYS = {"text", "start_ms", "end_ms", "avg_confidence", "words", "voice_command"}
WORD_KEYS = {"text", "start_ms", "end_ms", "probability"}
CONVERSATION_EXTRA_SEGMENT_KEYS = {"id", "speaker", "speaker_confidence", "speaker_role"}
CONVERSATION_EXTRA_WORD_KEYS = {"speaker", "speaker_confidence"}


@dataclass
class _Word:
    text: str
    start_ms: int
    end_ms: int
    probability: float


@dataclass
class _Segment:
    text: str
    start_ms: int
    end_ms: int
    avg_confidence: float
    words: list[_Word] = field(default_factory=list)


class _StubDiarization:
    """Attribution by exact (start_ms, end_ms) with a default fallback."""

    def __init__(
        self,
        table: dict[tuple[int, int], tuple[str | None, float | None]],
        default: tuple[str | None, float | None] = (None, None),
    ) -> None:
        self._table = table
        self._default = default
        self.segments: list[Any] = []

    def attribute(self, start_ms: int, end_ms: int) -> tuple[str | None, float | None]:
        return self._table.get((start_ms, end_ms), self._default)


class _StubMappingInference:
    def __init__(self, hypothesis: MappingHypothesis | None) -> None:
        self.current = hypothesis


def _segments() -> list[_Segment]:
    return [
        _Segment(
            text="доброго дня",
            start_ms=0,
            end_ms=1_000,
            avg_confidence=0.91,
            words=[
                _Word("доброго", 0, 500, 0.93),
                _Word("дня", 500, 1_000, 0.89),
            ],
        ),
        _Segment(
            text="болить голова",
            start_ms=1_000,
            end_ms=2_000,
            avg_confidence=0.84,
            words=[
                _Word("болить", 1_000, 1_500, 0.85),
                _Word("голова", 1_500, 2_000, 0.83),
            ],
        ),
    ]


def _ctx(
    *,
    mode: str = "dictation",
    diarization: Any | None = None,
    mapping_inference: Any | None = None,
) -> SessionContext:
    ctx = SessionContext(
        session_id=uuid4(),
        tenant_id=uuid4(),
        user_id=uuid4(),
        language="uk",
        prompt_id=uuid4(),
        prompt_text="",
        target_kind="generic",
        encounter_id=None,
        template_id=None,
        mode=mode,
        diarization=diarization,
        mapping_inference=mapping_inference,
    )
    ctx.finalized_segments = list(_segments())
    return ctx


# ── Dictation: the frozen pre-sprint-14 shape ────────────────────────


def test_dictation_segment_shape_is_unchanged() -> None:
    docs = _transcript_to_jsonb(_ctx())

    assert len(docs) == 2
    for doc in docs:
        assert set(doc) == SEGMENT_KEYS
        assert doc["voice_command"] is None
        for word in doc["words"]:
            assert set(word) == WORD_KEYS

    assert docs[0]["text"] == "доброго дня"
    assert docs[0]["start_ms"] == 0
    assert docs[0]["end_ms"] == 1_000
    assert docs[0]["avg_confidence"] == 0.91
    assert [w["text"] for w in docs[0]["words"]] == ["доброго", "дня"]


def test_dictation_with_a_diarizer_attached_still_has_no_speaker_keys() -> None:
    # mode is the switch, not the presence of a stream.
    ctx = _ctx(diarization=_StubDiarization({}, default=("S1", 0.9)))
    docs = _transcript_to_jsonb(ctx)
    assert all(set(doc) == SEGMENT_KEYS for doc in docs)


# ── Conversation: ids + speaker proposals ────────────────────────────


def _conversation_ctx(
    table: dict[tuple[int, int], tuple[str | None, float | None]],
    *,
    mapping: dict[str, str] | None = None,
) -> SessionContext:
    hypothesis = (
        MappingHypothesis(mapping=mapping, confidence=0.8, rationale="")
        if mapping is not None
        else None
    )
    return _ctx(
        mode="conversation",
        diarization=_StubDiarization(table),
        mapping_inference=_StubMappingInference(hypothesis),
    )


def test_conversation_adds_ids_speakers_and_roles() -> None:
    ctx = _conversation_ctx(
        {
            (0, 1_000): ("S1", 0.9),
            (0, 500): ("S1", 0.9),
            (500, 1_000): ("S1", 0.88),
            (1_000, 2_000): ("S2", 0.77),
            (1_000, 1_500): ("S2", 0.76),
            (1_500, 2_000): ("S2", 0.79),
        },
        mapping={"S1": "doctor", "S2": "patient"},
    )

    docs = _transcript_to_jsonb(ctx)

    assert all(set(doc) == SEGMENT_KEYS | CONVERSATION_EXTRA_SEGMENT_KEYS for doc in docs)
    for doc in docs:
        for word in doc["words"]:
            assert set(word) == WORD_KEYS | CONVERSATION_EXTRA_WORD_KEYS

    assert docs[0]["speaker"] == "S1"
    assert docs[0]["speaker_confidence"] == 0.9
    assert docs[0]["speaker_role"] == "doctor"
    assert docs[1]["speaker"] == "S2"
    assert docs[1]["speaker_role"] == "patient"

    assert [w["speaker"] for w in docs[0]["words"]] == ["S1", "S1"]
    assert [w["speaker_confidence"] for w in docs[1]["words"]] == [0.76, 0.79]


def test_segment_ids_are_unique_uuids() -> None:
    ctx = _conversation_ctx({}, mapping={"S1": "doctor"})
    docs = _transcript_to_jsonb(ctx)

    ids = [doc["id"] for doc in docs]
    assert len(set(ids)) == len(ids) == 2
    for value in ids:
        assert isinstance(value, str)
        assert str(UUID(value)) == value  # canonical UUID string


def test_unknown_and_null_speakers_survive_into_persistence() -> None:
    ctx = _conversation_ctx(
        {
            (0, 1_000): ("UNKNOWN", 0.2),
            (0, 500): ("UNKNOWN", 0.2),
            (500, 1_000): (None, None),
            # segment 2 and its words fall through to the (None, None) default
        },
        mapping={"S1": "doctor", "S2": "patient"},
    )

    docs = _transcript_to_jsonb(ctx)

    assert docs[0]["speaker"] == "UNKNOWN"
    assert docs[0]["speaker_confidence"] == 0.2
    assert docs[0]["speaker_role"] is None  # never guessed into a party
    assert docs[0]["words"][1]["speaker"] is None
    assert docs[0]["words"][1]["speaker_confidence"] is None

    assert docs[1]["speaker"] is None
    assert docs[1]["speaker_confidence"] is None
    assert docs[1]["speaker_role"] is None


def test_no_mapping_yet_still_emits_speakers_without_roles() -> None:
    ctx = _conversation_ctx({(0, 1_000): ("S1", 0.9)}, mapping=None)

    docs = _transcript_to_jsonb(ctx)

    assert docs[0]["speaker"] == "S1"
    assert docs[0]["speaker_role"] is None
    assert "id" in docs[0]
