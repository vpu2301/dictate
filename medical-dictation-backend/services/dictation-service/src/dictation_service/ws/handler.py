"""Main WebSocket session handler.

One coroutine runs the lifecycle per accepted upgrade:

- Accept the WS with the negotiated subprotocol.
- Wait for ``start_session`` (or ``start_session{resume_session_id}``).
- Spin up SessionContext + audio buffer + decoder + windower.
- Concurrently:
    * pump frames (audio + control)
    * tick the windower every ``window_tick_interval_ms``
    * heartbeat + idle watchdog + token-expiry watchdog
- On close / ``end_session`` / cap: run finalize.

Errors map to wire-level ``Error`` messages with the recoverability
flag from :mod:`protocol.error_catalogue`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from typing import Any
from uuid import UUID, uuid4

from fastapi import WebSocketDisconnect

from audit import Severity
from db import tenant_connection

from .. import audit_kinds
from ..audio import (
    GapPolicy,
    OpusDecodeError,
    OpusDecoder,
    SessionAudioBuffer,
    decode_pcm_view,
    gap_decision,
)
from ..audio.gap import GapDecision
from ..config import settings
from ..domain import repository
from ..inference import StreamingWindower
from ..protocol import (
    AudioFrame,
    BadMessageError,
    EndSession,
    Error,
    ErrorCode,
    Final,
    Partial,
    Pause,
    RefreshToken,
    Resume,
    RetransmitRange,
    SessionStarted,
    SessionTerminated,
    StartSession,
    SwitchSection,
    WarningMessage,
    decode_binary,
    decode_text,
    encode_server,
)
from ..protocol.error_catalogue import is_recoverable
from ..session.finalize import finalize_session
from ..session.heartbeat import (
    heartbeat_loop,
    idle_watchdog,
    token_expiry_watchdog,
)
from ..session.manager import (
    CapacityError,
    DuplicateSessionError,
    SessionContext,
)
from ..session.resume import (
    evaluate_resume,
    evaluate_retransmit,
)
from ..session.state import SessionState, assert_transition
from ..ws.upgrade import UpgradeContext

logger = logging.getLogger(__name__)


async def run_session(
    websocket: Any,  # starlette WebSocket
    *,
    upgrade: UpgradeContext,
    state: Any,
) -> None:
    """Top-level coroutine after the upgrade. Always closes cleanly."""
    await websocket.accept(subprotocol=upgrade.subprotocol)

    ctx: SessionContext | None = None
    try:
        ctx = await _wait_for_start(websocket, upgrade, state)
        if ctx is None:
            return  # already closed
        await _run_loop(ctx, websocket, state)
    except WebSocketDisconnect:
        if ctx is not None:
            await _on_client_disconnect(ctx, state)
    except Exception as exc:  # noqa: BLE001
        logger.exception("session.unhandled", exc_info=exc)
        with suppress(Exception):
            await websocket.send_text(
                encode_server(
                    Error(code=ErrorCode.INTERNAL, detail="internal error", recoverable=False)
                )
            )
        if ctx is not None:
            await _on_failed(ctx, state, kind="internal", detail=str(exc))


async def _wait_for_start(
    websocket: Any,
    upgrade: UpgradeContext,
    state: Any,
) -> SessionContext | None:
    """Read the first text message; expect StartSession or close."""
    try:
        text = await asyncio.wait_for(
            websocket.receive_text(),
            timeout=settings.ws_idle_close_after_no_session_s,
        )
    except TimeoutError:
        await _send_and_close(
            websocket,
            Error(code=ErrorCode.BAD_MESSAGE, detail="no start_session received"),
        )
        return None

    try:
        msg = decode_text(text)
    except BadMessageError as exc:
        await _send_and_close(
            websocket,
            Error(code=exc.code, detail=exc.detail, recoverable=is_recoverable(exc.code)),
        )
        return None

    if not isinstance(msg, StartSession):
        await _send_and_close(
            websocket,
            Error(code=ErrorCode.BAD_MESSAGE, detail="expected start_session first"),
        )
        return None

    if msg.resume_session_id is not None:
        return await _resume_session(websocket, upgrade, state, msg)
    return await _new_session(websocket, upgrade, state, msg)


# ── New session ──────────────────────────────────────────────────────


async def _new_session(
    websocket: Any,
    upgrade: UpgradeContext,
    state: Any,
    start: StartSession,
) -> SessionContext | None:
    if state.session_manager.total_count >= settings.per_worker_max_sessions:
        await _send_and_close(
            websocket,
            Error(
                code=ErrorCode.GPU_FULL,
                detail="worker at session capacity; retry shortly",
                recoverable=True,
            ),
            ws_code=1013,  # try again later
        )
        return None

    # Per-tenant active cap.
    async with tenant_connection(state.app_pool, upgrade.claims.tid) as conn:
        active = await repository.count_active_for_tenant(conn, tenant_id=upgrade.claims.tid)
    if active >= settings.per_tenant_max_active_sessions:
        await _send_and_close(
            websocket,
            Error(
                code=ErrorCode.RATE_LIMITED,
                detail="tenant active-session cap reached",
                recoverable=True,
            ),
        )
        return None

    # Fetch the requested prompt's text.
    prompt_text = await _fetch_prompt_text(state, upgrade.claims.tid, start.prompt_id)
    if prompt_text is None:
        await _send_and_close(
            websocket,
            Error(
                code=ErrorCode.BAD_MESSAGE,
                detail="prompt_id not found",
                recoverable=False,
            ),
        )
        return None

    session_id = uuid4()
    ctx = SessionContext(
        session_id=session_id,
        tenant_id=upgrade.claims.tid,
        user_id=upgrade.claims.sub,
        language=start.language,
        prompt_id=start.prompt_id,
        prompt_text=prompt_text,
        target_kind=start.target_kind,
        encounter_id=start.encounter_id,
        template_id=start.template_id,
        claims=upgrade.claims,
        token_exp_ts=upgrade.claims.exp,
    )
    ctx.ws = websocket
    ctx.state = SessionState.ACTIVE
    ctx.started_at = time.monotonic()
    ctx.buffer = SessionAudioBuffer(session_id=session_id)
    ctx.decoder = OpusDecoder()

    # Sprint-06: load template for section-aware dictation.
    # template_client uses a service-account bearer obtained from
    # Keycloak via the mdx-dictation client (sprint 04 §A); if neither
    # the client nor the bearer is available, the branch silently
    # skips and the session runs without section-swap support.
    template_client = getattr(state, "template_client", None)
    s2s_bearer = getattr(state, "s2s_bearer", None)
    if start.template_id is not None and template_client is not None and s2s_bearer:
        try:
            tpl = await template_client.fetch(template_id=start.template_id, bearer=s2s_bearer)
            if tpl is not None:
                ctx.template_doc = tpl
                if tpl.sections:
                    first = tpl.sections[0]
                    ctx.active_section_id = first.get("id")
                    ctx.active_section_prompt = first.get("asr_prompt")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "template_load.failed",
                extra={
                    "session_id": str(session_id),
                    "template_id": str(start.template_id),
                    "error_class": type(exc).__name__,
                },
            )

    try:
        await state.session_manager.register(ctx)
    except CapacityError:
        ctx.buffer.close()
        await _send_and_close(
            websocket,
            Error(code=ErrorCode.GPU_FULL, recoverable=True),
            ws_code=1013,
        )
        return None
    except DuplicateSessionError:
        ctx.buffer.close()
        await _send_and_close(
            websocket,
            Error(code=ErrorCode.SESSION_NOT_FOUND, recoverable=False),
        )
        return None

    async with tenant_connection(state.app_pool, ctx.tenant_id) as conn:
        await repository.insert_session(
            conn,
            session_id=session_id,
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            language=ctx.language,
            prompt_id=ctx.prompt_id,
            target_kind=ctx.target_kind,
            encounter_id=ctx.encounter_id,
            template_id=ctx.template_id,
            worker_id=settings.worker_id,
        )

    await state.audit_writer.write_event(
        tenant_id=ctx.tenant_id,
        kind=audit_kinds.SESSION_STARTED,
        actor_sub=ctx.user_id,
        target_kind="dictation_session",
        target_id=str(session_id),
        payload={
            "language": ctx.language,
            "target_kind": ctx.target_kind,
            "prompt_id": str(ctx.prompt_id),
            "encounter_id": str(ctx.encounter_id) if ctx.encounter_id else None,
        },
        severity=Severity.INFO,
    )

    await websocket.send_text(
        encode_server(
            SessionStarted(
                session_id=session_id,
                resumed=False,
                last_committed_seq=0,
                committed_audio_until_ms=0,
                server_time_ms=int(time.time() * 1000),
                model=state.engine.model_name,
                language=ctx.language,
            )
        )
    )
    return ctx


# ── Resume ────────────────────────────────────────────────────────────


async def _resume_session(
    websocket: Any,
    upgrade: UpgradeContext,
    state: Any,
    start: StartSession,
) -> SessionContext | None:
    sid = start.resume_session_id
    assert sid is not None
    live_attached = await state.session_manager.has_live_for(sid)
    async with tenant_connection(state.app_pool, upgrade.claims.tid) as conn:
        outcome = await evaluate_resume(
            conn,
            state.redis,
            session_id=sid,
            requesting_user=upgrade.claims.sub,
            requesting_tenant=upgrade.claims.tid,
            live_session_attached=live_attached,
        )
    if not outcome.allowed:
        # Uniform failure: never leak the precise reason.
        await _send_and_close(
            websocket,
            Error(
                code=ErrorCode.SESSION_NOT_FOUND,
                detail="session not found",
                recoverable=False,
            ),
        )
        return None

    row = outcome.row
    assert row is not None
    ctx: SessionContext | None = state.session_manager.get(sid)
    if ctx is None:
        # The session is in DB but no in-process context — worker
        # restart case. We don't recover the buffer; tell the client to
        # use sprint-3 batch path.
        await _send_and_close(
            websocket,
            Error(
                code=ErrorCode.WORKER_FAILED,
                detail="worker restarted; recover via local buffer",
                recoverable=False,
            ),
        )
        return None

    ctx.ws = websocket
    ctx.network_drop_count += 1
    ctx.state = SessionState.ACTIVE
    ctx.touch()

    await state.audit_writer.write_event(
        tenant_id=ctx.tenant_id,
        kind=audit_kinds.SESSION_RESUMED,
        actor_sub=ctx.user_id,
        target_kind="dictation_session",
        target_id=str(sid),
        payload={"network_drop_count": ctx.network_drop_count},
        severity=Severity.INFO,
    )

    await websocket.send_text(
        encode_server(
            SessionStarted(
                session_id=sid,
                resumed=True,
                last_committed_seq=ctx.received_seqs_hwm + 1,
                committed_audio_until_ms=ctx.buffer.total_ms if ctx.buffer else 0,
                server_time_ms=int(time.time() * 1000),
                model=state.engine.model_name,
                language=ctx.language,
            )
        )
    )
    return ctx


# ── Main session loop ────────────────────────────────────────────────


async def _run_loop(ctx: SessionContext, websocket: Any, state: Any) -> None:
    windower = StreamingWindower(
        base_prompt=ctx.prompt_text,
        language=ctx.language,
    )
    stop = asyncio.Event()

    async def _on_idle(_ctx: SessionContext) -> None:
        with suppress(Exception):
            await websocket.close(code=1011)

    hb_task = asyncio.create_task(heartbeat_loop(ctx))
    idle_task = asyncio.create_task(idle_watchdog(ctx, on_idle=_on_idle))
    tok_task = asyncio.create_task(token_expiry_watchdog(ctx))
    tick_task = asyncio.create_task(_window_loop(ctx, windower, state, stop))

    try:
        while True:
            try:
                msg = await websocket.receive()
            except WebSocketDisconnect:
                raise
            ctx.touch()

            # Per Starlette WS framing the dict carries 'type' = 'websocket.receive'
            # plus either 'text' or 'bytes'. The framing layer above us already
            # rejects unknown frame kinds.
            if "text" in msg and msg["text"] is not None:
                cont = await _on_text(ctx, websocket, state, msg["text"], windower)
                if not cont:
                    break
            elif "bytes" in msg and msg["bytes"] is not None:
                await _on_binary(ctx, websocket, state, msg["bytes"])
            elif msg.get("type") == "websocket.disconnect":
                raise WebSocketDisconnect(code=msg.get("code", 1006))
    finally:
        stop.set()
        for t in (hb_task, idle_task, tok_task, tick_task):
            t.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await t

        # Hard 60-min cap finalize — handled inline in _on_binary too.
        if ctx.state == SessionState.ACTIVE and ctx.buffer is not None and _exceeds_hard_cap(ctx):
            await _finalize_normal(ctx, state, reason="cap_reached")


# ── Frame handlers ───────────────────────────────────────────────────


async def _on_text(
    ctx: SessionContext,
    websocket: Any,
    state: Any,
    text: str,
    windower: StreamingWindower,
) -> bool:
    """Process one text frame. Return False if the loop should exit."""
    try:
        msg = decode_text(text)
    except BadMessageError as exc:
        await websocket.send_text(
            encode_server(
                Error(
                    code=exc.code,
                    detail=exc.detail,
                    recoverable=is_recoverable(exc.code),
                )
            )
        )
        return True  # stay open

    if isinstance(msg, EndSession):
        await _finalize_normal(ctx, state, reason="normal")
        return False

    if isinstance(msg, Pause):
        if ctx.state != SessionState.ACTIVE:
            await websocket.send_text(
                encode_server(Error(code=ErrorCode.PAUSE_STATE_MISMATCH, recoverable=True))
            )
            return True
        ctx.state = SessionState.PAUSED
        ctx.paused_at = time.monotonic()
        return True

    if isinstance(msg, Resume):
        if ctx.state != SessionState.PAUSED:
            await websocket.send_text(
                encode_server(Error(code=ErrorCode.PAUSE_STATE_MISMATCH, recoverable=True))
            )
            return True
        ctx.state = SessionState.ACTIVE
        ctx.paused_at = None
        return True

    if isinstance(msg, RetransmitRange):
        decision = evaluate_retransmit(
            from_seq=msg.from_seq,
            to_seq=msg.to_seq,
            hwm=ctx.received_seqs_hwm,
        )
        if decision.too_large:
            await websocket.send_text(
                encode_server(
                    Error(
                        code=ErrorCode.RETRANSMIT_TOO_LARGE,
                        detail=f"max {settings.retransmit_max_range_frames} frames",
                        recoverable=True,
                    )
                )
            )
        # Either way we accept and dedup as frames arrive; nothing else
        # to do here.
        return True

    if isinstance(msg, SwitchSection):
        # Sprint-06: swap ASR prompt for the next Whisper window.
        if ctx.template_doc is None:
            await websocket.send_text(
                encode_server(
                    Error(
                        code=ErrorCode.BAD_MESSAGE,
                        detail="no template loaded for this session",
                        recoverable=True,
                    )
                )
            )
            return True
        from ..integrations.template_client import section_prompt

        resolved = section_prompt(ctx.template_doc, msg.section_id)
        if resolved is None:
            await websocket.send_text(
                encode_server(
                    Error(
                        code=ErrorCode.BAD_MESSAGE,
                        detail=f"section {msg.section_id!r} not in template",
                        recoverable=True,
                    )
                )
            )
            return True
        new_prompt, new_section_name = resolved
        from_section = ctx.active_section_id
        ctx.active_section_id = msg.section_id
        ctx.active_section_prompt = new_prompt
        # Propagate to the windower's base prompt — next tick reads it.
        if windower is not None:
            windower.base_prompt = new_prompt
        await state.audit_writer.write_event(
            tenant_id=ctx.tenant_id,
            kind=audit_kinds.SECTION_SWITCHED,
            actor_sub=ctx.user_id,
            target_kind="dictation_session",
            target_id=str(ctx.session_id),
            payload={
                "from_section": from_section or "",
                "to_section": msg.section_id,
                "to_section_name": new_section_name,
                "reason": msg.reason,
            },
            severity=Severity.INFO,
        )
        return True

    if isinstance(msg, RefreshToken):
        # Validate the new token and replace claims/exp.
        from auth import verify_token

        try:
            new_claims = await verify_token(
                msg.token,
                jwks_cache=state.jwks_cache,
                expected_audience=settings.auth_audience,
                expected_issuer=settings.auth_issuer,
                clock_skew_seconds=settings.auth_clock_skew_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            await websocket.send_text(
                encode_server(
                    Error(
                        code=ErrorCode.AUTH_INVALID,
                        detail=f"refresh rejected: {type(exc).__name__}",
                        recoverable=False,
                    )
                )
            )
            return True
        # The new token must be for the same user + tenant.
        if new_claims.sub != ctx.user_id or new_claims.tid != ctx.tenant_id:
            await websocket.send_text(
                encode_server(
                    Error(
                        code=ErrorCode.AUTH_INVALID,
                        detail="refresh subject/tenant mismatch",
                        recoverable=False,
                    )
                )
            )
            return True
        ctx.claims = new_claims
        ctx.token_exp_ts = new_claims.exp
        return True

    if isinstance(msg, StartSession):
        # Already started — reject a duplicate.
        await websocket.send_text(
            encode_server(
                Error(
                    code=ErrorCode.BAD_MESSAGE,
                    detail="session already started",
                    recoverable=False,
                )
            )
        )
        return True

    return True


async def _on_binary(ctx: SessionContext, websocket: Any, state: Any, data: bytes) -> None:
    """Process one binary audio frame."""
    if ctx.state == SessionState.PAUSED:
        await websocket.send_text(
            encode_server(
                Error(
                    code=ErrorCode.PAUSE_STATE_MISMATCH,
                    detail="session paused; resume first",
                    recoverable=True,
                )
            )
        )
        return

    try:
        frame: AudioFrame = decode_binary(data)
    except BadMessageError as exc:
        # Oversized or malformed binary → close.
        await websocket.send_text(
            encode_server(Error(code=exc.code, detail=exc.detail, recoverable=False))
        )
        with suppress(Exception):
            await websocket.close(code=1003)
        return

    decision = gap_decision(ctx.expected_seq, frame.seq, policy=GapPolicy())
    if decision.decision == GapDecision.DUPLICATE:
        return  # silently drop
    if decision.decision == GapDecision.REQUEST_RETRANSMIT:
        await websocket.send_text(
            encode_server(
                Error(
                    code=ErrorCode.GAP_DETECTED,
                    detail=f"expected {ctx.expected_seq}, got {frame.seq}",
                    recoverable=True,
                )
            )
        )
        return
    if decision.decision == GapDecision.PAD_SILENCE and ctx.buffer is not None:
        ctx.buffer.insert_silence(decision.pad_samples)

    # Decode + write.
    if ctx.decoder is None or ctx.buffer is None:
        return
    try:
        pcm = ctx.decoder.decode(frame.opus)
    except OpusDecodeError as exc:
        await websocket.send_text(
            encode_server(
                Error(
                    code=ErrorCode.AUDIO_DECODE_FAILED,
                    detail=exc.args[0] if exc.args else "decode failed",
                    recoverable=not exc.fatal,
                )
            )
        )
        if exc.fatal:
            await _on_failed(ctx, state, kind="worker_failed", detail="opus_decode")
            with suppress(Exception):
                await websocket.close(code=1011)
        return
    ctx.buffer.write(pcm)
    ctx.expected_seq = decision.next_expected_seq
    ctx.received_seqs_hwm = max(ctx.received_seqs_hwm, frame.seq)

    # Hard cap.
    if _exceeds_hard_cap(ctx):
        await _finalize_normal(ctx, state, reason="cap_reached")
        with suppress(Exception):
            await websocket.close(code=1000)


# ── Background tasks ─────────────────────────────────────────────────


async def _window_loop(
    ctx: SessionContext,
    windower: StreamingWindower,
    state: Any,
    stop: asyncio.Event,
) -> None:
    """Tick the windower every N ms; emit partials + finals."""
    interval = settings.window_tick_interval_ms / 1000.0
    while not stop.is_set() and ctx.ws is not None:
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return
        except TimeoutError:
            pass
        if ctx.state != SessionState.ACTIVE or ctx.buffer is None:
            continue
        slice_ = windower.next_slice(buffer_total_ms=ctx.buffer.total_ms)
        if slice_ is None:
            continue
        try:
            pcm = decode_pcm_view(ctx.buffer, slice_.start_ms, slice_.end_ms)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "windower.buffer_read_failed",
                extra={"session_id": str(ctx.session_id), "error": str(exc)},
            )
            continue
        prompt = windower.build_prompt_for_next_window()
        t0 = time.monotonic()
        try:
            window_result = await state.inference_queue.submit(
                pcm,
                language=ctx.language,
                prompt=ctx.prompt_text,
                prev_text=prompt,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "windower.inference_failed",
                extra={"session_id": str(ctx.session_id), "error": str(exc)},
            )
            continue
        infer_seconds = time.monotonic() - t0
        tick = windower.integrate(
            window_segments=getattr(window_result, "segments", []),
            window_no_speech_prob=getattr(window_result, "no_speech_prob", 1.0),
            window_start_ms=slice_.start_ms,
            window_end_ms=slice_.end_ms,
            infer_seconds=infer_seconds,
            pcm_for_vad=pcm,
        )
        await _emit_tick(ctx, tick)


async def _emit_tick(ctx: SessionContext, tick: Any) -> None:
    """Serialize windower output to the wire."""
    if ctx.ws is None:
        return
    if tick.boundary_uncertainty > settings.aligner_boundary_uncertainty_threshold:
        with suppress(Exception):
            await ctx.ws.send_text(
                encode_server(
                    WarningMessage(
                        session_id=ctx.session_id,
                        code="low_confidence",
                        detail=f"boundary={tick.boundary_uncertainty:.2f}",
                    )
                )
            )
    if tick.new_partial is not None:
        ctx.out_seq += 1
        partial_age_ms = max(0, ctx.buffer.total_ms - tick.new_partial.end_ms) if ctx.buffer else 0
        ctx.partial_latencies_ms.append(partial_age_ms)
        with suppress(Exception):
            await ctx.ws.send_text(
                encode_server(
                    Partial(
                        session_id=ctx.session_id,
                        seq=ctx.out_seq,
                        text=tick.new_partial.text,
                        start_ms=tick.new_partial.start_ms,
                        end_ms=tick.new_partial.end_ms,
                        words=list(tick.new_partial.words),
                        avg_confidence=tick.new_partial.avg_confidence,
                    )
                )
            )
    for seg in tick.new_finals:
        ctx.out_seq += 1
        final_age_ms = max(0, ctx.buffer.total_ms - seg.end_ms) if ctx.buffer else 0
        ctx.final_latencies_ms.append(final_age_ms)
        ctx.finalized_segments.append(seg)
        with suppress(Exception):
            await ctx.ws.send_text(
                encode_server(
                    Final(
                        session_id=ctx.session_id,
                        seq=ctx.out_seq,
                        text=seg.text,
                        start_ms=seg.start_ms,
                        end_ms=seg.end_ms,
                        words=list(seg.words),
                        avg_confidence=seg.avg_confidence,
                        voice_command=None,
                    )
                )
            )


# ── Closure paths ────────────────────────────────────────────────────


async def _send_and_close(websocket: Any, error: Error, *, ws_code: int = 1008) -> None:
    with suppress(Exception):
        await websocket.send_text(encode_server(error))
    with suppress(Exception):
        await websocket.close(code=ws_code)


def _exceeds_hard_cap(ctx: SessionContext) -> bool:
    if ctx.buffer is None:
        return False
    return bool(ctx.buffer.total_ms >= settings.session_hard_cap_minutes * 60 * 1000)


async def _finalize_normal(ctx: SessionContext, state: Any, *, reason: str) -> None:
    try:
        result = await finalize_session(
            ctx=ctx,
            app_pool=state.app_pool,
            audit_writer=state.audit_writer,
            audio_store=state.audio_store,
            envelope=state.envelope,
            reason=reason,
            purge_audio=settings.demo_audio_purge_on_finalize,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("finalize.failed", exc_info=exc)
        await _on_failed(ctx, state, kind="internal", detail=f"finalize: {exc}")
        return

    if ctx.ws is not None:
        with suppress(Exception):
            await ctx.ws.send_text(
                encode_server(
                    SessionTerminated(
                        session_id=ctx.session_id,
                        reason=reason,
                        finalized_audio_file_id=result.audio_file_id,
                    )
                )
            )
        with suppress(Exception):
            await ctx.ws.close(code=1000)
    await state.session_manager.unregister(ctx.session_id)


async def _on_client_disconnect(ctx: SessionContext, state: Any) -> None:
    """Move to reconnecting; the 30-min abandon timer takes it from there."""
    if ctx.state in {SessionState.FINALIZED, SessionState.ABANDONED, SessionState.FAILED}:
        return
    with suppress(Exception):
        assert_transition(ctx.state, SessionState.RECONNECTING)
    ctx.state = SessionState.RECONNECTING
    ctx.ws = None
    async with tenant_connection(state.app_pool, ctx.tenant_id) as conn:
        await repository.update_status(
            conn,
            session_id=ctx.session_id,
            new_status=SessionState.RECONNECTING,
        )
    # The abandon-timer task is started lazily here so the
    # session-manager doesn't need a global scheduler.
    asyncio.create_task(_abandon_after_idle(ctx, state))


async def _abandon_after_idle(ctx: SessionContext, state: Any) -> None:
    timeout = settings.session_idle_abandon_minutes * 60
    while True:
        if ctx.state != SessionState.RECONNECTING:
            return  # resumed or finalized
        elapsed = time.monotonic() - ctx.last_active_at
        if elapsed >= timeout:
            break
        await asyncio.sleep(min(30.0, timeout - elapsed))
    if ctx.state != SessionState.RECONNECTING:
        return
    ctx.state = SessionState.ABANDONED
    async with tenant_connection(state.app_pool, ctx.tenant_id) as conn:
        await repository.update_status(
            conn, session_id=ctx.session_id, new_status=SessionState.ABANDONED
        )
    await state.audit_writer.write_event(
        tenant_id=ctx.tenant_id,
        kind=audit_kinds.SESSION_ABANDONED,
        actor_sub=ctx.user_id,
        target_kind="dictation_session",
        target_id=str(ctx.session_id),
        payload={"idle_minutes": settings.session_idle_abandon_minutes},
        severity=Severity.INFO,
    )
    if ctx.buffer is not None:
        ctx.buffer.close()
        ctx.buffer = None
    await state.session_manager.unregister(ctx.session_id)


async def _on_failed(ctx: SessionContext, state: Any, *, kind: str, detail: str) -> None:
    ctx.state = SessionState.FAILED
    async with tenant_connection(state.app_pool, ctx.tenant_id) as conn:
        await repository.update_status(
            conn,
            session_id=ctx.session_id,
            new_status=SessionState.FAILED,
            error_kind=kind,
            error_detail=detail[:1024],
        )
    await state.audit_writer.write_event(
        tenant_id=ctx.tenant_id,
        kind=audit_kinds.SESSION_FAILED,
        actor_sub=ctx.user_id,
        target_kind="dictation_session",
        target_id=str(ctx.session_id),
        payload={"reason": kind, "detail": detail[:200]},
        severity=Severity.ERROR,
    )
    if ctx.buffer is not None:
        ctx.buffer.close()
        ctx.buffer = None
    await state.session_manager.unregister(ctx.session_id)


async def _fetch_prompt_text(state: Any, tenant_id: UUID, prompt_id: UUID) -> str | None:
    async with state.app_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT prompt_text FROM medical_prompts WHERE id = $1",
            prompt_id,
        )
    return str(row["prompt_text"]) if row is not None else None
