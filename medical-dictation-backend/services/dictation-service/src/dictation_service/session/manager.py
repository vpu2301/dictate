"""Per-process session registry + in-memory contexts.

Every live session has one :class:`SessionContext` here. The context
owns the audio buffer reference, the Whisper context, the WS connection
(or None during reconnect), and the sequence-cursor bookkeeping. DB
state mirrors the in-memory state — the manager is responsible for
keeping them aligned.

Manager is process-local. Multi-process workers (sprint 16) keep
sessions affine to a single process via Redis routing.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from auth import Claims

from .state import SessionState

logger = logging.getLogger(__name__)


@dataclass
class SessionContext:
    """Everything the session loop needs in one place.

    ``ws`` is the current WebSocket connection (or None while
    reconnecting). ``buffer`` is the :class:`SessionAudioBuffer` (sprint
    04 day 4). ``finalized_text`` is the running last-N-tokens used as
    Whisper's ``initial_prompt`` for the next window.
    """

    session_id: UUID
    tenant_id: UUID
    user_id: UUID
    language: str
    prompt_id: UUID
    prompt_text: str
    target_kind: str
    encounter_id: UUID | None
    template_id: UUID | None
    template_text: str | None = None

    state: SessionState = SessionState.CREATING
    state_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    # Wire-level state
    ws: Any | None = None  # WebSocket — typed Any so we don't import starlette here
    expected_seq: int = 0
    received_seqs_hwm: int = -1  # high-water mark for dedup
    out_seq: int = 0  # server-emitted message seq

    # Audio buffer + decoder
    buffer: Any | None = None  # SessionAudioBuffer
    decoder: Any | None = None  # OpusDecoder

    # Windowing + inference
    last_partial_emit_ms: int = 0
    last_window_cursor_ms: int = 0
    finalized_segments: list[Any] = field(default_factory=list)  # list[Segment]
    # The session's StreamingWindower. Held here so every teardown path
    # (normal, cap, failure, abandon) can flush the words still provisional
    # at end-of-session instead of dropping them — see
    # `StreamingWindower.flush_provisional`.
    windower: Any | None = None  # StreamingWindower
    # Serialises windower mutation. The tick loop is still live when
    # EndSession triggers finalize, so the end-of-session drain would
    # otherwise race a normal tick and corrupt the windower's cursor.
    window_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    # Timing
    created_at: float = field(default_factory=time.monotonic)
    last_active_at: float = field(default_factory=time.monotonic)
    started_at: float | None = None
    paused_at: float | None = None
    network_drop_count: int = 0

    # Auth
    claims: Claims | None = None
    token_exp_ts: int | None = None

    # Metrics accumulators
    partial_latencies_ms: list[int] = field(default_factory=list)
    final_latencies_ms: list[int] = field(default_factory=list)

    # Sprint-06: section-aware dictation
    template_doc: Any | None = None  # TemplateDoc (avoiding import cycle)
    active_section_id: str | None = None
    active_section_prompt: str | None = None

    # Sprint-14: conversation mode + protocol v2. The diarization stream
    # and mapping inference live on the context so an in-process resume
    # keeps the speaker timeline exactly like `finalized_segments`.
    mode: str = "dictation"  # 'dictation' | 'conversation'
    protocol_version: int = 1
    bearer: str | None = None  # raw token — draft creation forwards the clinician's identity
    capacity_weight: int = 1
    patient_id: UUID | None = None  # resolved by the consent gate (conversation only)
    diarization: Any | None = None  # DiarizationStream (Any: torch-free import path)
    mapping_inference: Any | None = None  # SpeakerMappingInference
    mapping_manual: bool = False  # a SetSpeakerMapping arrived; inference frozen
    # Honesty metrics (Grafana DER proxy). Counted on COMMITTED words
    # only — partials are re-emitted every tick until they commit.
    unknown_speaker_words: int = 0
    labeled_speaker_words: int = 0
    pending_speaker_words: int = 0
    speaker_mapping_updates: int = 0
    speaker_mapping_manual_sets: int = 0

    def touch(self) -> None:
        self.last_active_at = time.monotonic()

    def is_active(self) -> bool:
        return self.state == SessionState.ACTIVE


class SessionManager:
    """Thread-safe (asyncio-safe) registry of live sessions on this process."""

    def __init__(self, *, max_sessions: int) -> None:
        self._sessions: dict[UUID, SessionContext] = {}
        self._lock = asyncio.Lock()
        self._max_sessions = max_sessions
        # Sprint 16 deployment: scale-in drain. When True the worker
        # admits NOTHING new; live sessions run to completion. Set by
        # the preStop hook (POST /internal/drain) — Kubernetes then
        # waits (terminationGracePeriodSeconds) until active sessions
        # hit zero before the pod dies. One-way by design: a draining
        # pod is already condemned by the autoscaler.
        self._draining = False

    @property
    def draining(self) -> bool:
        return self._draining

    def begin_drain(self) -> None:
        self._draining = True
        logger.warning(
            "session_manager.draining",
            extra={"active": self.active_count, "total_weight": self.total_weight},
        )

    @property
    def active_count(self) -> int:
        return sum(1 for s in self._sessions.values() if s.state == SessionState.ACTIVE)

    @property
    def total_count(self) -> int:
        return len(self._sessions)

    @property
    def total_weight(self) -> int:
        """Mode-aware load: dictation = 1, conversation = its configured
        weight (2 by default — two resident models). The cap compares
        weight, not headcount: 4 dictation OR 2 conversation OR a mix."""
        return sum(s.capacity_weight for s in self._sessions.values())

    def fits(self, weight: int) -> bool:
        if self._draining:
            return False
        return self.total_weight + weight <= self._max_sessions

    async def register(self, ctx: SessionContext) -> None:
        async with self._lock:
            if self._draining:
                # Same client semantics as gpu_full: this worker has no
                # room — reconnect and land on another pod.
                raise CapacityError("worker is draining for scale-in; no new sessions")
            if self.total_weight + ctx.capacity_weight > self._max_sessions:
                raise CapacityError(
                    f"worker at capacity (weight {self.total_weight}"
                    f"+{ctx.capacity_weight} > {self._max_sessions})"
                )
            if ctx.session_id in self._sessions:
                raise DuplicateSessionError(f"session_id {ctx.session_id} already attached")
            self._sessions[ctx.session_id] = ctx
            logger.info(
                "session.registered",
                extra={"session_id": str(ctx.session_id), "tenant_id": str(ctx.tenant_id)},
            )

    async def unregister(self, session_id: UUID) -> SessionContext | None:
        async with self._lock:
            ctx = self._sessions.pop(session_id, None)
            if ctx is not None:
                logger.info("session.unregistered", extra={"session_id": str(session_id)})
            return ctx

    def get(self, session_id: UUID) -> SessionContext | None:
        return self._sessions.get(session_id)

    def all(self) -> list[SessionContext]:
        return list(self._sessions.values())

    async def has_live_for(self, session_id: UUID) -> bool:
        """Single-tab guard. True iff a live WS is attached to this session."""
        ctx = self._sessions.get(session_id)
        return ctx is not None and ctx.ws is not None


class CapacityError(Exception):
    """Raised on register() when per-worker max is reached."""


class DuplicateSessionError(Exception):
    """Raised on register() when a session_id is already attached to a live ctx."""
