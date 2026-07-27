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

from .. import audit_kinds, metrics
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
from ..diarization.engine import DiarizationUnavailableError
from ..diarization.mapping import SpeakerMappingInference
from ..domain import consents, encounters, repository
from ..inference import StreamingWindower
from ..notifications import emit_dictation_completed
from ..protocol import (
    PROTOCOL_VERSION_V1,
    PROTOCOL_VERSION_V2,
    AudioFrame,
    BadMessageError,
    EndSession,
    Error,
    ErrorCode,
    Final,
    FinalV2,
    Partial,
    PartialV2,
    Pause,
    RefreshToken,
    Resume,
    RetransmitRange,
    SessionStarted,
    SessionStartedV2,
    SessionTerminated,
    SetSpeakerMapping,
    SpeakerMappingUpdated,
    StartSession,
    StartSessionV2,
    SwitchSection,
    TokenTiming,
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
            protocol_version=upgrade.protocol_version,
        )
        return None

    try:
        msg = decode_text(text, upgrade.protocol_version)
    except BadMessageError as exc:
        await _send_and_close(
            websocket,
            Error(code=exc.code, detail=exc.detail, recoverable=is_recoverable(exc.code)),
            protocol_version=upgrade.protocol_version,
        )
        return None

    if not isinstance(msg, StartSession):
        await _send_and_close(
            websocket,
            Error(code=ErrorCode.BAD_MESSAGE, detail="expected start_session first"),
            protocol_version=upgrade.protocol_version,
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
    # Sprint 14: mode exists only on the v2 wire; v1 is always dictation.
    mode = start.mode if isinstance(start, StartSessionV2) else "dictation"
    weight = settings.conversation_session_weight if mode == "conversation" else 1

    # Mode-aware capacity: a conversation session runs two models, so it
    # costs `conversation_session_weight` slots (ADR-0034 §capacity).
    if not state.session_manager.fits(weight):
        metrics.session_drops.add(1, {"reason": "gpu_full"})
        await _send_and_close(
            websocket,
            Error(
                code=ErrorCode.GPU_FULL,
                detail="worker at session capacity; retry shortly",
                recoverable=True,
            ),
            ws_code=1013,  # try again later
            protocol_version=upgrade.protocol_version,
        )
        return None

    # Conversation records the PATIENT: hard-require an encounter (the
    # consent lives on the encounter's patient). Same error code as a
    # missing consent — the fix for both is the consent flow.
    if mode == "conversation" and start.encounter_id is None:
        await _refuse_consent(
            websocket, state, upgrade, detail="conversation mode requires encounter_id"
        )
        return None

    # Per-tenant active cap + encounter linkage + recording consent
    # (one tenant-scoped trip).
    encounter_status: str | None = None
    recording_consent: dict[str, Any] | None = None
    async with tenant_connection(state.app_pool, upgrade.claims.tid) as conn:
        active = await repository.count_active_for_tenant(conn, tenant_id=upgrade.claims.tid)
        if start.encounter_id is not None:
            encounter_status = await encounters.fetch_encounter_status(
                conn, encounter_id=start.encounter_id
            )
            if mode == "conversation":
                recording_consent = await consents.fetch_recording_consent(
                    conn, encounter_id=start.encounter_id
                )
    if active >= settings.per_tenant_max_active_sessions:
        await _send_and_close(
            websocket,
            Error(
                code=ErrorCode.RATE_LIMITED,
                detail="tenant active-session cap reached",
                recoverable=True,
            ),
            protocol_version=upgrade.protocol_version,
        )
        return None

    # S11 step 02: a named encounter must exist in-tenant (RLS makes a
    # foreign encounter look nonexistent) and not be cancelled — rejected
    # here, before a single audio frame is accepted. Conversation mode
    # additionally needs the visit to still be open (0058).
    if start.encounter_id is not None:
        gate = encounters.encounter_gate(encounter_status, mode=mode)
        if gate is not None:
            if gate is ErrorCode.ENCOUNTER_INVALID:
                detail = "encounter not found in this tenant"
            elif encounter_status == "cancelled":
                detail = "encounter is cancelled; dictation is not allowed"
            else:
                detail = (
                    f"visit is already {encounter_status}; "
                    "start a new visit to record a conversation"
                )
            await _send_and_close(
                websocket,
                Error(code=gate, detail=detail, recoverable=False),
                protocol_version=upgrade.protocol_version,
            )
            return None

    # S14: conversation requires a granted 'recording' consent for the
    # encounter's patient — refused before a single audio frame.
    if mode == "conversation":
        if consents.consent_gate(recording_consent) is not None:
            await _refuse_consent(
                websocket,
                state,
                upgrade,
                encounter_id=start.encounter_id,
                detail="no granted 'recording' consent for the encounter's patient",
            )
            return None
        # Diarizer must actually be loadable — fail loud at start, never
        # mid-consultation with silently unlabeled audio.
        try:
            await state.diarization_engine.ensure_loaded()
        except DiarizationUnavailableError as exc:
            await _send_and_close(
                websocket,
                Error(code=ErrorCode.WORKER_FAILED, detail=str(exc), recoverable=True),
                ws_code=1013,
                protocol_version=upgrade.protocol_version,
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
            protocol_version=upgrade.protocol_version,
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
        mode=mode,
        protocol_version=upgrade.protocol_version,
        bearer=upgrade.bearer,
        capacity_weight=weight,
        patient_id=(recording_consent or {}).get("patient_id"),
    )
    ctx.ws = websocket
    ctx.state = SessionState.ACTIVE
    ctx.started_at = time.monotonic()
    ctx.buffer = SessionAudioBuffer(session_id=session_id)
    ctx.decoder = OpusDecoder()
    if mode == "conversation":
        ctx.diarization = state.diarization_engine.new_stream()
        ctx.mapping_inference = SpeakerMappingInference(language=ctx.language)

    # Sprint-06: load template for section-aware dictation. Sprint-14
    # wires it for real: the client is built in main_deps and the call
    # forwards the CLINICIAN's own bearer (the repo's cross-service
    # pattern) — the old service-account plumbing never existed.
    template_client = getattr(state, "template_client", None)
    s2s_bearer = upgrade.bearer
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
            protocol_version=upgrade.protocol_version,
        )
        return None
    except DuplicateSessionError:
        ctx.buffer.close()
        await _send_and_close(
            websocket,
            Error(code=ErrorCode.SESSION_NOT_FOUND, recoverable=False),
            protocol_version=upgrade.protocol_version,
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
            mode=ctx.mode,
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
            "mode": ctx.mode,
            "protocol_version": ctx.protocol_version,
        },
        severity=Severity.INFO,
    )

    metrics.conversation_sessions.add(1, {"mode": ctx.mode})

    started_kwargs: dict[str, Any] = {
        "session_id": session_id,
        "resumed": False,
        "last_committed_seq": 0,
        "committed_audio_until_ms": 0,
        "server_time_ms": int(time.time() * 1000),
        "model": state.engine.model_name,
        "language": ctx.language,
    }
    if ctx.protocol_version == PROTOCOL_VERSION_V2:
        started = SessionStartedV2(**started_kwargs, mode=ctx.mode)  # type: ignore[arg-type]
    else:
        started = SessionStarted(**started_kwargs)  # type: ignore[assignment]
    await websocket.send_text(encode_server(started, ctx.protocol_version))
    return ctx


async def _refuse_consent(
    websocket: Any,
    state: Any,
    upgrade: UpgradeContext,
    *,
    encounter_id: UUID | None = None,
    detail: str = "",
) -> None:
    """Audit + refuse a conversation start without a recording consent."""
    metrics.session_drops.add(1, {"reason": "consent_refused"})
    await state.audit_writer.write_event(
        tenant_id=upgrade.claims.tid,
        kind=audit_kinds.CONSENT_REFUSED,
        actor_sub=upgrade.claims.sub,
        target_kind="encounter",
        target_id=str(encounter_id) if encounter_id else "none",
        payload={"detail": detail},
        severity=Severity.WARN,
    )
    await _send_and_close(
        websocket,
        Error(code=ErrorCode.CONSENT_REQUIRED, detail=detail, recoverable=False),
        protocol_version=upgrade.protocol_version,
    )


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
            protocol_version=upgrade.protocol_version,
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
            protocol_version=upgrade.protocol_version,
        )
        return None

    # S14: a session is exactly one wire version for its lifetime. A
    # reconnect on a different subprotocol gets the uniform refusal
    # (a v1 tab must not receive v2 frames from a v2-born session).
    if ctx.protocol_version != upgrade.protocol_version:
        await _send_and_close(
            websocket,
            Error(
                code=ErrorCode.SESSION_NOT_FOUND,
                detail="session not found",
                recoverable=False,
            ),
            protocol_version=upgrade.protocol_version,
        )
        return None

    ctx.ws = websocket
    ctx.network_drop_count += 1
    ctx.state = SessionState.ACTIVE
    ctx.bearer = upgrade.bearer  # reconnects may carry a fresher token
    ctx.touch()
    # Sprint 04 declared this instrument but never emitted it, so
    # DictationReconnectRateHigh could not fire (sprint-14 deployment pass).
    metrics.reconnects.add(1, {"worker_id": settings.worker_id, "mode": ctx.mode})

    await state.audit_writer.write_event(
        tenant_id=ctx.tenant_id,
        kind=audit_kinds.SESSION_RESUMED,
        actor_sub=ctx.user_id,
        target_kind="dictation_session",
        target_id=str(sid),
        payload={"network_drop_count": ctx.network_drop_count},
        severity=Severity.INFO,
    )

    resumed_kwargs: dict[str, Any] = {
        "session_id": sid,
        "resumed": True,
        "last_committed_seq": ctx.received_seqs_hwm + 1,
        "committed_audio_until_ms": ctx.buffer.total_ms if ctx.buffer else 0,
        "server_time_ms": int(time.time() * 1000),
        "model": state.engine.model_name,
        "language": ctx.language,
    }
    if ctx.protocol_version == PROTOCOL_VERSION_V2:
        resumed = SessionStartedV2(**resumed_kwargs, mode=ctx.mode)  # type: ignore[arg-type]
    else:
        resumed = SessionStarted(**resumed_kwargs)  # type: ignore[assignment]
    await websocket.send_text(encode_server(resumed, ctx.protocol_version))
    return ctx


# ── Main session loop ────────────────────────────────────────────────


async def _run_loop(ctx: SessionContext, websocket: Any, state: Any) -> None:
    windower = StreamingWindower(
        base_prompt=ctx.prompt_text,
        language=ctx.language,
    )
    # Reachable from the finalize paths, which do not take the windower.
    ctx.windower = windower
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
        msg = decode_text(text, ctx.protocol_version)
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
        await apply_pause(ctx, state)
        return True

    if isinstance(msg, Resume):
        if ctx.state != SessionState.PAUSED:
            await websocket.send_text(
                encode_server(Error(code=ErrorCode.PAUSE_STATE_MISMATCH, recoverable=True))
            )
            return True
        await apply_resume(ctx, state)
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

    if isinstance(msg, SetSpeakerMapping):
        # Sprint-14: the clinician's manual doctor/patient assignment —
        # authoritative from this moment; inference stops permanently.
        if ctx.mode != "conversation" or ctx.mapping_inference is None:
            await websocket.send_text(
                encode_server(
                    Error(
                        code=ErrorCode.BAD_MESSAGE,
                        detail="set_speaker_mapping is only valid in conversation mode",
                        recoverable=True,
                    ),
                    ctx.protocol_version,
                )
            )
            return True
        ctx.mapping_inference.freeze(dict(msg.mapping))
        ctx.mapping_manual = True
        ctx.speaker_mapping_manual_sets += 1
        metrics.speaker_mapping_updates.add(1, {"source": "manual"})
        await state.audit_writer.write_event(
            tenant_id=ctx.tenant_id,
            kind=audit_kinds.SPEAKER_MAPPING_MANUAL_SET,
            actor_sub=ctx.user_id,
            target_kind="dictation_session",
            target_id=str(ctx.session_id),
            payload={"mapping": dict(msg.mapping)},
            severity=Severity.INFO,
        )
        # NB: no ctx.out_seq bump — SpeakerMappingUpdated carries no
        # `seq`, and incrementing here would punch a gap in the
        # partial/final sequence the client tracks.
        with suppress(Exception):
            await websocket.send_text(
                encode_server(
                    SpeakerMappingUpdated(
                        session_id=ctx.session_id,
                        mapping=dict(msg.mapping),
                        confidence=1.0,
                        rationale="manual override",
                        manual=True,
                    ),
                    ctx.protocol_version,
                )
            )
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
        ctx.bearer = msg.token  # finalize-time draft creation uses the freshest token
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

# How many ticks in a row may fail before the session is failed outright
# rather than left "recording" with a transcriber that produces nothing.
_MAX_CONSECUTIVE_TICK_FAILURES = 3


async def _window_loop(
    ctx: SessionContext,
    windower: StreamingWindower,
    state: Any,
    stop: asyncio.Event,
) -> None:
    """Tick the windower every N ms; emit partials + finals.

    This loop IS the transcript: if it stops, the session keeps accepting
    audio, keeps looking healthy to the clinician, and finalizes empty.
    It ran as a bare ``create_task`` with no error path, so a single
    unhandled exception in one tick killed the transcript for the rest of
    the session and left nothing in the log (that is exactly how the
    ``TokenTiming`` serialization defect above stayed invisible). Every
    tick is now guarded: one bad tick is logged and skipped, the loop
    survives, and a genuinely broken session fails the session rather
    than silently transcribing nothing.
    """
    interval = settings.window_tick_interval_ms / 1000.0
    consecutive_failures = 0
    while not stop.is_set() and ctx.ws is not None:
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return
        except TimeoutError:
            pass
        if ctx.state != SessionState.ACTIVE or ctx.buffer is None:
            continue
        try:
            await _window_tick(ctx, windower, state)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            consecutive_failures += 1
            logger.exception(
                "windower.tick_failed",
                extra={
                    "session_id": str(ctx.session_id),
                    "mode": ctx.mode,
                    "error_class": type(exc).__name__,
                    "consecutive": consecutive_failures,
                },
            )
            if consecutive_failures >= _MAX_CONSECUTIVE_TICK_FAILURES:
                # Never let a session go on "recording" with a dead
                # transcriber — tell the clinician instead.
                await _on_failed(
                    ctx,
                    state,
                    kind="worker_failed",
                    detail=f"window loop failed {consecutive_failures}x: {type(exc).__name__}",
                )
                return
        else:
            consecutive_failures = 0


async def _drain_final_window(ctx: SessionContext, state: Any) -> None:
    """Transcribe the audio tail that is shorter than one hop.

    ``next_slice`` only yields a window once a full hop of fresh audio has
    arrived, so whatever is shorter than that when the session ends never
    reaches the model. Run one forced window to pick it up.

    Best-effort: the transcript that IS committed must never be lost to a
    failure in here, so every error is swallowed and finalize continues.
    """
    windower = ctx.windower
    if windower is None or ctx.buffer is None:
        return
    try:
        await _window_tick(ctx, windower, state, force=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "windower.final_drain_failed",
            extra={"session_id": str(ctx.session_id), "error_class": type(exc).__name__},
        )


async def _window_tick(
    ctx: SessionContext,
    windower: StreamingWindower,
    state: Any,
    *,
    force: bool = False,
) -> None:
    """One windower tick: slice → inference (+ diarization) → emit."""
    if ctx.buffer is None:
        return
    async with ctx.window_lock:
        await _window_tick_locked(ctx, windower, state, force=force)


async def _window_tick_locked(
    ctx: SessionContext,
    windower: StreamingWindower,
    state: Any,
    *,
    force: bool = False,
) -> None:
    if ctx.buffer is None:
        return
    slice_ = windower.next_slice(buffer_total_ms=ctx.buffer.total_ms, force=force)
    if slice_ is None:
        return
    try:
        pcm = decode_pcm_view(ctx.buffer, slice_.start_ms, slice_.end_ms)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "windower.buffer_read_failed",
            extra={"session_id": str(ctx.session_id), "error": str(exc)},
        )
        return
    prompt = windower.build_prompt_for_next_window()
    t_window = time.monotonic()
    t0 = t_window
    try:
        window_result = await state.inference_queue.submit(
            pcm,
            language=ctx.language,
            # `prompt` already carries the specialty prompt: the windower
            # composes base + finalized-tail and budgets the pair to
            # `prompt_max_tokens`. Also passing ctx.prompt_text as `prompt`
            # made the engine's `_combine_prompts` concatenate the specialty
            # prompt to itself, so every window's initial_prompt opened with
            # it twice. A repeated initial_prompt is a documented Whisper
            # repetition/hallucination trigger, and the duplicate also ate
            # the token budget meant for real decoded context.
            prompt=None,
            prev_text=prompt,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "windower.inference_failed",
            extra={"session_id": str(ctx.session_id), "error": str(exc)},
        )
        return
    infer_seconds = time.monotonic() - t0
    # Sprint-04 declared these instruments but never emitted them, so the
    # streaming dashboard and its latency alerts had no data (found in the
    # sprint-14 deployment pass). Conversation mode makes them mandatory:
    # the co-tenancy claim is exactly "dictation latency survives".
    mode_attrs = {"worker_id": settings.worker_id, "mode": ctx.mode}
    metrics.window_inference_ms.record(infer_seconds * 1000.0, mode_attrs)
    window_audio_seconds = max(0, slice_.end_ms - slice_.start_ms) / 1000.0
    if infer_seconds > 0:
        metrics.rtf.record(window_audio_seconds / infer_seconds, mode_attrs)
    tick = windower.integrate(
        window_segments=getattr(window_result, "segments", []),
        window_no_speech_prob=getattr(window_result, "no_speech_prob", 1.0),
        window_start_ms=slice_.start_ms,
        window_end_ms=slice_.end_ms,
        infer_seconds=infer_seconds,
        pcm_for_vad=pcm,
    )

    # Sprint-14: diarize the same window (conversation sessions).
    # Runs in a thread so the tick loop never blocks the event loop;
    # measured ≤ ~60 ms/window on CPU (ADR-0034). Failure degrades to
    # unlabeled words — text delivery always wins over labels.
    if ctx.mode == "conversation" and ctx.diarization is not None:
        try:
            await asyncio.to_thread(
                ctx.diarization.process_window,
                pcm,
                window_start_ms=slice_.start_ms,
            )
            metrics.diarization_window_ms.record(
                ctx.diarization.last_window_seconds * 1000.0,
                {"worker_id": settings.worker_id},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "diarization.window_failed",
                extra={"session_id": str(ctx.session_id), "error_class": type(exc).__name__},
            )
        # Inference AND diarization for this window — the sum is what has
        # to fit the tick budget, and it is what the fleet decision
        # (single mixed pool vs a dedicated conversation pool) is judged on.
        metrics.conversation_window_total_ms.record(
            (time.monotonic() - t_window) * 1000.0,
            {"worker_id": settings.worker_id},
        )

    await _emit_tick(ctx, tick)
    if ctx.mode == "conversation":
        await _maybe_emit_mapping_update(ctx, state)


def _wire_words(words: Any) -> list[TokenTiming]:
    """Project inference ``WordTiming``s onto the wire's ``TokenTiming``.

    The two models are field-identical but are DIFFERENT classes
    (``asr_models.WordTiming`` is the inference contract, ``TokenTiming``
    the protocol one), and pydantic v2 does not coerce one BaseModel
    instance into another. Passing the raw list raised ``ValidationError``
    inside ``_emit_tick`` — which killed the whole window loop on the
    first partial, so every streaming session emitted nothing and
    finalized an empty transcript (sprint-04 latent; found S14).
    """
    return [
        TokenTiming(
            text=w.text,
            start_ms=w.start_ms,
            end_ms=w.end_ms,
            probability=w.probability,
        )
        for w in words
    ]


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
                    ),
                    ctx.protocol_version,
                )
            )
    v2 = ctx.protocol_version == PROTOCOL_VERSION_V2
    hint = _current_mapping_hint(ctx)
    if tick.new_partial is not None:
        ctx.out_seq += 1
        partial_age_ms = max(0, ctx.buffer.total_ms - tick.new_partial.end_ms) if ctx.buffer else 0
        ctx.partial_latencies_ms.append(partial_age_ms)
        # Split by mode: "dictation p95 survives co-tenancy" is only checkable
        # if dictation latency is separable from conversation latency on the
        # same worker (sprint-14 deployment).
        metrics.partial_latency_ms.record(
            partial_age_ms, {"worker_id": settings.worker_id, "mode": ctx.mode}
        )
        partial_kwargs: dict[str, Any] = {
            "session_id": ctx.session_id,
            "seq": ctx.out_seq,
            "text": tick.new_partial.text,
            "start_ms": tick.new_partial.start_ms,
            "end_ms": tick.new_partial.end_ms,
            "words": _wire_words(tick.new_partial.words),
            "avg_confidence": tick.new_partial.avg_confidence,
        }
        if v2:
            # Partials are re-emitted every tick until they commit, so
            # their labels are NOT counted — only committed words are
            # (see metrics.conversation_words).
            speaker, speaker_conf = _attribute_segment(ctx, tick.new_partial, count=False)
            message: Any = PartialV2(
                **partial_kwargs,
                speaker=speaker,
                speaker_confidence=speaker_conf,
                speaker_mapping_hint=hint,
            )
        else:
            message = Partial(**partial_kwargs)
        with suppress(Exception):
            await ctx.ws.send_text(encode_server(message, ctx.protocol_version))
    for seg in tick.new_finals:
        ctx.out_seq += 1
        final_age_ms = max(0, ctx.buffer.total_ms - seg.end_ms) if ctx.buffer else 0
        ctx.final_latencies_ms.append(final_age_ms)
        metrics.final_latency_ms.record(
            final_age_ms, {"worker_id": settings.worker_id, "mode": ctx.mode}
        )
        ctx.finalized_segments.append(seg)
        final_kwargs: dict[str, Any] = {
            "session_id": ctx.session_id,
            "seq": ctx.out_seq,
            "text": seg.text,
            "start_ms": seg.start_ms,
            "end_ms": seg.end_ms,
            "words": _wire_words(seg.words),
            "avg_confidence": seg.avg_confidence,
            "voice_command": None,
        }
        if v2:
            speaker, speaker_conf = _attribute_segment(ctx, seg, count=True)
            _feed_mapping_inference(ctx, seg)
            final_message: Any = FinalV2(
                **final_kwargs,
                speaker=speaker,
                speaker_confidence=speaker_conf,
                speaker_mapping_hint=hint,
            )
        else:
            final_message = Final(**final_kwargs)
        with suppress(Exception):
            await ctx.ws.send_text(encode_server(final_message, ctx.protocol_version))


def _attribute_segment(
    ctx: SessionContext, seg: Any, *, count: bool
) -> tuple[str | None, float | None]:
    """Segment-level speaker proposal from the diarization timeline.
    None while labels trail the text (pre-bootstrap or past the
    diarized frontier) — the wire contract allows late labels.

    ``count`` gates the honesty metrics: only committed (final) words
    are counted, since a partial is re-emitted on every tick.
    """
    if ctx.diarization is None:
        return None, None
    speaker, conf = ctx.diarization.attribute(int(seg.start_ms), int(seg.end_ms))
    if count:
        n_words = len(getattr(seg, "words", []) or [])
        outcome = "unknown" if speaker == "UNKNOWN" else ("labeled" if speaker else "pending")
        if outcome == "unknown":
            ctx.unknown_speaker_words += n_words
        elif outcome == "labeled":
            ctx.labeled_speaker_words += n_words
        else:
            ctx.pending_speaker_words += n_words
        metrics.conversation_words.add(
            n_words, {"outcome": outcome, "tenant_id": str(ctx.tenant_id)}
        )
    return speaker, conf


def _feed_mapping_inference(ctx: SessionContext, seg: Any) -> None:
    """Committed words feed the doctor/patient inference (word-level
    attribution — a segment straddling a turn boundary must not vote
    all its words for one side)."""
    if ctx.mapping_inference is None or ctx.diarization is None or ctx.mapping_manual:
        return
    ctx.mapping_inference.observe_segments(ctx.diarization.segments)
    for w in getattr(seg, "words", []) or []:
        speaker, _conf = ctx.diarization.attribute(int(w.start_ms), int(w.end_ms))
        ctx.mapping_inference.observe_word(w.text, speaker)


def _current_mapping_hint(ctx: SessionContext) -> dict[str, Any] | None:
    if ctx.mapping_inference is None:
        return None
    current = ctx.mapping_inference.current
    return dict(current.mapping) if current is not None else None


async def _maybe_emit_mapping_update(ctx: SessionContext, state: Any) -> None:
    """Emit SpeakerMappingUpdated when the hypothesis changes (never
    after a manual set — freeze() makes evaluate() return None)."""
    if ctx.mapping_inference is None or ctx.mapping_manual or ctx.ws is None:
        return
    hypothesis = ctx.mapping_inference.evaluate()
    if hypothesis is None:
        return
    ctx.speaker_mapping_updates += 1
    metrics.speaker_mapping_updates.add(1, {"source": "inferred"})
    await state.audit_writer.write_event(
        tenant_id=ctx.tenant_id,
        kind=audit_kinds.SPEAKER_MAPPING_INFERRED,
        actor_sub=ctx.user_id,
        target_kind="dictation_session",
        target_id=str(ctx.session_id),
        payload={
            "mapping": dict(hypothesis.mapping),
            "confidence": hypothesis.confidence,
            "rationale": hypothesis.rationale,
        },
        severity=Severity.INFO,
    )
    with suppress(Exception):
        await ctx.ws.send_text(
            encode_server(
                SpeakerMappingUpdated(
                    session_id=ctx.session_id,
                    mapping=dict(hypothesis.mapping),
                    confidence=hypothesis.confidence,
                    rationale=hypothesis.rationale,
                    manual=False,
                ),
                ctx.protocol_version,
            )
        )


# ── Closure paths ────────────────────────────────────────────────────


async def _send_and_close(
    websocket: Any,
    error: Error,
    *,
    ws_code: int = 1008,
    protocol_version: int = PROTOCOL_VERSION_V1,
) -> None:
    # `Error` has an identical schema in both unions today, but encode
    # at the session's negotiated version anyway: every other send site
    # does, and a future v2-only Error field must not silently vanish.
    with suppress(Exception):
        await websocket.send_text(encode_server(error, protocol_version))
    with suppress(Exception):
        await websocket.close(code=ws_code)


def _exceeds_hard_cap(ctx: SessionContext) -> bool:
    if ctx.buffer is None:
        return False
    return bool(ctx.buffer.total_ms >= settings.session_hard_cap_minutes * 60 * 1000)


async def _finalize_normal(ctx: SessionContext, state: Any, *, reason: str) -> None:
    # Single funnel for every completion path (normal, cap_reached,
    # force_finalize), so the tail is picked up once regardless of which
    # one got here. Must precede finalize_session, which only serialises
    # what is already committed.
    await _drain_final_window(ctx, state)
    try:
        result = await finalize_session(
            ctx=ctx,
            app_pool=state.app_pool,
            audit_writer=state.audit_writer,
            audio_store=state.audio_store,
            envelope=state.envelope,
            reason=reason,
            purge_audio=settings.demo_audio_purge_on_finalize,
            nlp_client=getattr(state, "nlp_client", None),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("finalize.failed", exc_info=exc)
        await _on_failed(ctx, state, kind="internal", detail=f"finalize: {exc}")
        return

    # Sprint-14: conversation sessions land a report draft through the
    # EXISTING POST /v1/reports (sprint-08 hand-off; no parallel write
    # path). Completion paths only — failure/abandon never draft.
    if ctx.mode == "conversation":
        from ..session.draft import create_conversation_draft  # local import — avoid cycle

        await create_conversation_draft(ctx, state, finalize_result=result)

    # Emitted here rather than inside finalize_session: every reason that
    # reaches THIS function is a session that completed (normal,
    # cap_reached, force_finalize). The failure and abandon paths call
    # the same finalizer and must not produce a completion receipt.
    await emit_dictation_completed(
        state.redis,
        tenant_id=ctx.tenant_id,
        session_id=ctx.session_id,
        user_id=ctx.user_id,
        duration_ms=result.duration_ms,
        segments=result.transcript_segments,
    )

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


async def apply_pause(ctx: SessionContext, state: Any) -> None:
    """active → paused, persisted.

    The pause used to live only in ``ctx.state``, which made it invisible to
    every other process: ``GET /dictate/sessions`` still reported the session
    as active, and so did the per-tenant capacity count. Anything that has to
    reason about "is this session still going" reads the DB, so the DB has to
    know. Shared with the HTTP pause endpoint, which is the only way back for
    a client whose socket is gone.
    """
    assert_transition(ctx.state, SessionState.PAUSED)
    ctx.state = SessionState.PAUSED
    ctx.paused_at = time.monotonic()
    async with tenant_connection(state.app_pool, ctx.tenant_id) as conn:
        await repository.update_status(
            conn, session_id=ctx.session_id, new_status=SessionState.PAUSED
        )


async def apply_resume(ctx: SessionContext, state: Any) -> None:
    """paused → active, persisted. Mirror of :func:`apply_pause`."""
    assert_transition(ctx.state, SessionState.ACTIVE)
    ctx.state = SessionState.ACTIVE
    ctx.paused_at = None
    async with tenant_connection(state.app_pool, ctx.tenant_id) as conn:
        await repository.update_status(
            conn, session_id=ctx.session_id, new_status=SessionState.ACTIVE
        )


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
