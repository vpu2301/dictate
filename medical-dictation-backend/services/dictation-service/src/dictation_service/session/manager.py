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

import numpy as np

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
    out_seq: int = 0             # server-emitted message seq

    # Audio buffer + decoder
    buffer: Any | None = None  # SessionAudioBuffer
    decoder: Any | None = None  # OpusDecoder

    # Windowing + inference
    last_partial_emit_ms: int = 0
    last_window_cursor_ms: int = 0
    finalized_segments: list[Any] = field(default_factory=list)  # list[Segment]

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

    @property
    def active_count(self) -> int:
        return sum(1 for s in self._sessions.values() if s.state == SessionState.ACTIVE)

    @property
    def total_count(self) -> int:
        return len(self._sessions)

    async def register(self, ctx: SessionContext) -> None:
        async with self._lock:
            if self.total_count >= self._max_sessions:
                raise CapacityError(
                    f"worker at capacity ({self._max_sessions} sessions)"
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
