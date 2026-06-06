"""Pydantic discriminated unions for the medical-dictation.v1 wire protocol.

Strict (`extra="forbid"`) on every model. The strictness is the point:
sprint 14 will fork to `medical-dictation.v2` for diarization fields; a
v1 client receiving a v2 message must reject it cleanly, and an attacker
must not be able to smuggle `is_admin`-style fields through.

Field naming follows the canonical spec at docs/api/dictation-ws-v1.md.
Reordering or renaming is a breaking change.
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, NonNegativeFloat, NonNegativeInt

from .error_catalogue import ErrorCode

# Bumping this is the only way to break v1 compatibly — see ADR-0012.
PROTOCOL_VERSION_V1: int = 1


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ── Word + segment timing ─────────────────────────────────────────────


class TokenTiming(_StrictModel):
    """One word in a partial/final segment."""

    text: str
    start_ms: NonNegativeInt
    end_ms: NonNegativeInt
    probability: float = Field(ge=0.0, le=1.0)


# ──────────────────────────────────────────────────────────────────────
# Server → client messages
# ──────────────────────────────────────────────────────────────────────


class SessionStarted(_StrictModel):
    type: Literal["session_started"] = "session_started"
    protocol_version: int = PROTOCOL_VERSION_V1
    session_id: UUID
    resumed: bool = False
    last_committed_seq: int = 0
    committed_audio_until_ms: NonNegativeInt = 0
    server_time_ms: NonNegativeInt
    model: str
    language: str = Field(pattern="^(uk|en)$")


class Partial(_StrictModel):
    """A provisional segment. May be revised on the next window."""

    type: Literal["partial"] = "partial"
    session_id: UUID
    seq: NonNegativeInt
    text: str
    start_ms: NonNegativeInt
    end_ms: NonNegativeInt
    words: list[TokenTiming] = Field(default_factory=list)
    avg_confidence: float = Field(ge=0.0, le=1.0)


class Final(_StrictModel):
    """A committed segment. Will not be revised. ``voice_command`` slot
    is reserved for sprint 05; always ``null`` in sprint 04."""

    type: Literal["final"] = "final"
    session_id: UUID
    seq: NonNegativeInt
    text: str
    start_ms: NonNegativeInt
    end_ms: NonNegativeInt
    words: list[TokenTiming] = Field(default_factory=list)
    avg_confidence: float = Field(ge=0.0, le=1.0)
    is_provisional: Literal[False] = False
    voice_command: object | None = None  # sprint-05 will populate


class VoiceCommand(_StrictModel):
    """Detached voice-command notification (sprint 05 emits, sprint 04 reserves)."""

    type: Literal["voice_command"] = "voice_command"
    session_id: UUID
    seq: NonNegativeInt
    command: str
    args: dict[str, str] = Field(default_factory=dict)


class Warning_(_StrictModel):
    """Non-fatal warning. Frontend may surface to the clinician."""

    type: Literal["warning"] = "warning"
    session_id: UUID
    code: str
    detail: str = ""


class Heartbeat(_StrictModel):
    type: Literal["heartbeat"] = "heartbeat"
    server_time_ms: NonNegativeInt


class TokenExpiring(_StrictModel):
    type: Literal["token_expiring"] = "token_expiring"
    expires_in_s: NonNegativeInt


class SessionTerminated(_StrictModel):
    type: Literal["session_terminated"] = "session_terminated"
    session_id: UUID
    reason: str  # "normal" | "cap_reached" | "token_expired" | "worker_failure" | ...
    finalized_audio_file_id: UUID | None = None


class Error(_StrictModel):
    """Wire-level error. Connection may stay open if ``recoverable``."""

    type: Literal["error"] = "error"
    code: ErrorCode
    detail: str = ""
    recoverable: bool = False


ServerMessage = Annotated[
    SessionStarted
    | Partial
    | Final
    | VoiceCommand
    | Warning_
    | Heartbeat
    | TokenExpiring
    | SessionTerminated
    | Error,
    Field(discriminator="type"),
]


# ──────────────────────────────────────────────────────────────────────
# Client → server messages
# ──────────────────────────────────────────────────────────────────────


class StartSession(_StrictModel):
    type: Literal["start_session"] = "start_session"
    protocol_version: int = PROTOCOL_VERSION_V1
    prompt_id: UUID
    language: str = Field(pattern="^(uk|en)$")
    target_kind: str = Field(default="generic")
    encounter_id: UUID | None = None
    template_id: UUID | None = None
    resume_session_id: UUID | None = None


class RefreshToken(_StrictModel):
    type: Literal["refresh_token"] = "refresh_token"
    token: str


class EndSession(_StrictModel):
    type: Literal["end_session"] = "end_session"


class Pause(_StrictModel):
    type: Literal["pause"] = "pause"


class Resume(_StrictModel):
    type: Literal["resume"] = "resume"


class RetransmitRange(_StrictModel):
    """Client request to retransmit binary frames in [from_seq, to_seq).

    Server is permissive: ranges already-committed are deduped silently.
    Ranges > MD_RETRANSMIT_MAX_RANGE_FRAMES are rejected.
    """

    type: Literal["retransmit_range"] = "retransmit_range"
    from_seq: NonNegativeInt
    to_seq: NonNegativeInt


class SwitchSection(_StrictModel):
    """Sprint-06 additive: switch the active template section.

    The next Whisper window will use the section's ASR prompt. The
    field is additive in v1 (ADR-0016 amendment) — v1 clients that
    never send it are unaffected. Backend validates the section_id
    belongs to the template loaded for this session; unknown
    section_id yields a server ``Error{code: bad_message}``.
    """

    type: Literal["switch_section"] = "switch_section"
    section_id: str
    reason: Literal["voice_command", "user_click", "programmatic"] = "voice_command"


ClientMessage = Annotated[
    StartSession | RefreshToken | EndSession | Pause | Resume | RetransmitRange | SwitchSection,
    Field(discriminator="type"),
]


# ──────────────────────────────────────────────────────────────────────
# Binary audio frame (decoded by the codec, not Pydantic)
# ──────────────────────────────────────────────────────────────────────


class AudioFrame(_StrictModel):
    """Parsed binary frame. The wire is 4-byte BE seq || opaque Opus bytes.

    Not a JSON message — synthesised by ``codec.decode_binary``.
    """

    type: Literal["audio_frame"] = "audio_frame"
    seq: NonNegativeInt
    opus: bytes
