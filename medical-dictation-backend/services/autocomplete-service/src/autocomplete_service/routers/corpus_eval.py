"""/corpus/eval — server-side persistence for the WER eval recorder.

Closes the recorder's "takes exist only in this tab" gap (the sprint-21
screen was deliberately client-only; populating the corpus meant a manual
ZIP + repo commit). Now:

  GET    /corpus/eval/script            the recording script (server copy —
                                        the SPA renders what this serves)
  GET    /corpus/eval/takes             the tenant's persisted takes
  PUT    /corpus/eval/takes/{id}        upload/replace one take
  GET    /corpus/eval/takes/{id}/audio  the stored WAV, for playback
  DELETE /corpus/eval/takes/{id}        remove a take
  GET    /corpus/eval/export            one ZIP in eval/corpus/v1/ layout
                                        covering EVERY take in the tenant

All behind `corpus.review` — the trust circle that already sees candidate
phrases. Committing the export into eval/corpus/v1/ stays an operator step
(the corpus lives in git), but the archive now covers everyone's work, not
one browser session.

THE SCRIPTED-ONLY INVARIANT, NOW SERVER-ENFORCED: an upload must name a
script_id present in eval_script.SCRIPT, and the stored transcript is the
SERVER's text for that line — a client cannot smuggle free-form audio in as
corpus text. The audio itself is a clinician reading a synthetic script;
the PII sweep at commit time stays a backstop, not a load-bearing wall.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field

from auth import Claims
from db import tenant_connection

from .. import audit_kinds, eval_archive, eval_instructions, eval_lines, eval_script
from .. import eval_repository as eval_repo
from ..deps import get_state, requires
from ..eval_wav import WavFormatError, parse_wav
from .phrases import _check_rate_limit

router = APIRouter(prefix="/corpus/eval", tags=["corpus"])

MIN_DURATION_MS = 300
MAX_DURATION_MS = 120_000
# 4 MiB of WAV → ~5.6M base64 chars; the field cap refuses larger payloads
# before any decode happens.
MAX_AUDIO_B64_CHARS = 6_000_000


class EvalScriptRowDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    subset: str | None
    language: str
    specialty: str
    say: str
    transcript: str  # gold text; equals `say` when they don't differ
    condition: str | None  # suggested recording condition, if the row has one
    # Where the line came from (0091): 'builtin' is vendored in the repo,
    # 'authored' was written in the console, 'adhoc' was spoken first and
    # written down after. Only the last two can be edited or removed, which
    # is what `editable` says without the client re-deriving it.
    source: str = "builtin"
    editable: bool = False
    # 'dev' or 'test' (0092). The recorder filters on it, and the coverage
    # panel counts the two sets apart — a dev line and a holdout line are not
    # interchangeable work.
    dataset: str = "test"
    # Recorded in BOTH conditions (0095, Epic D). The queue renders it as
    # "1/2 умов записано" — a paired line is not finished at one take.
    paired: bool = False


class EvalScriptDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str
    dictation_source: str
    subsets: list[str]
    conditions: list[str]
    items: list[EvalScriptRowDTO]


class EvalTakeDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    script_id: str
    script_version: str
    recorded_by: UUID
    language: str
    specialty: str
    subset: str | None
    condition: str
    #: The recordist stated this take really was recorded in that condition
    #: (0094). A take without it is not "recorded" — see save_take.
    condition_confirmed: bool = True
    duration_ms: int
    audio_sha256: str
    size_bytes: int
    #: The human's "брак" mark (0096). Cleared by re-recording.
    flagged_bad: bool = False
    flagged_note: str | None = None
    created_at: datetime
    updated_at: datetime


class EvalTakeListDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[EvalTakeDTO]
    total_duration_ms: int


class SaveTakeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    condition: Literal["headset", "laptop-mic", "phone-speaker-distance", "noisy"]
    audio_wav_base64: str = Field(min_length=64, max_length=MAX_AUDIO_B64_CHARS)
    # Echo of the line the client showed on screen. Optional, but when present
    # it must match the server's script — a mismatch means the client rendered
    # a drifted local copy, and storing that audio under the server's text
    # would silently corrupt the corpus.
    say: str | None = Field(default=None, max_length=500)
    # The browser's input-device label. Journalled, never stored on the take
    # itself: it describes the ATTEMPT, and §1.2 wants speaker and device
    # separable as variance components when the same line is read twice.
    device: str | None = Field(default=None, max_length=120)
    # Epic C: "yes, I really recorded this in that condition". The recorder
    # asks before the upload; without it the take is refused rather than
    # stored with an unverified label, because an unverified condition is
    # exactly what made the per-condition table meaningless.
    condition_confirmed: bool = False


class DiscardTakeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    condition: Literal["headset", "laptop-mic", "phone-speaker-distance", "noisy"]
    duration_ms: int = Field(ge=0, le=MAX_DURATION_MS)
    device: str | None = Field(default=None, max_length=120)
    # Free-form-ish, capped: 'retake', 'noise', 'misread', 'interrupted'.
    reason: str | None = Field(default=None, max_length=64)


def _take_dto(row: asyncpg.Record) -> EvalTakeDTO:
    return EvalTakeDTO(
        id=row["id"],
        script_id=row["script_id"],
        script_version=row["script_version"],
        recorded_by=row["recorded_by"],
        language=row["language"],
        specialty=row["specialty"],
        subset=row["subset"],
        condition=row["condition"],
        condition_confirmed=bool(row["condition_confirmed"]),
        duration_ms=int(row["duration_ms"]),
        audio_sha256=row["audio_sha256"],
        size_bytes=int(row["size_bytes"]),
        flagged_bad=bool(row["flagged_bad"]),
        flagged_note=row["flagged_note"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@router.get("/script", response_model=EvalScriptDTO)
async def eval_script_view(
    claims: Annotated[Claims, Depends(requires("corpus.review", "phrase"))],
) -> EvalScriptDTO:
    """The recording script: the vendored lines plus the tenant's own
    (0091). The server copy is the source of truth — the upload route
    validates against this same merged view, so a recorder rendering this
    response can never produce a refused take."""
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        lines = await eval_lines.all_lines(conn)
    return EvalScriptDTO(
        version=eval_script.SCRIPT_VERSION,
        dictation_source=eval_script.DICTATION_SOURCE,
        subsets=list(eval_script.SUBSETS),
        conditions=list(eval_script.CONDITIONS),
        items=[
            EvalScriptRowDTO(
                id=line.script_id,
                subset=line.subset,
                language=line.language,
                specialty=line.specialty,
                say=line.say,
                transcript=line.transcript,
                condition=line.condition,
                source=line.source,
                editable=line.editable,
                dataset=line.dataset,
                paired=line.paired,
            )
            for line in lines
        ],
    )


class ReplicaDTO(BaseModel):
    """One replica, everything the recording station needs — Epic C.

    Named after the spec's `GET /replicas/{id}`; it lives under this
    service's own prefix so the corpus surface stays one namespace.
    """

    model_config = ConfigDict(extra="forbid")
    id: str
    subset: str | None
    language: str
    specialty: str
    say: str
    transcript: str
    source: str
    editable: bool
    dataset: str
    paired: bool
    #: The condition THIS replica is to be recorded in. Null means nobody
    #: decided, and the console leaves the control unbound rather than
    #: inventing a default.
    condition: str | None
    #: Composed server-side from the condition and the category (0094). The
    #: SPA renders this text and hardcodes none of it.
    recording_instructions: str
    #: The two halves, so the console can style the condition sentence
    #: differently where it matters (phone + noise is easy to skim past).
    condition_instructions: str
    category_instructions: str
    #: Always true since Epic C — the recordist must state the condition
    #: they actually used before a take is accepted. A field rather than a
    #: constant because turning it off is a policy change somebody should
    #: have to make visibly.
    condition_confirmed_required: bool = True
    #: Which conditions already have a take. A paired replica is finished at
    #: two; everything else at one.
    conditions_recorded: list[str]


@router.get("/script/{script_id}", response_model=ReplicaDTO)
async def replica_detail(
    script_id: str,
    claims: Annotated[Claims, Depends(requires("corpus.review", "phrase"))],
    lang_ui: Annotated[str, Query(max_length=5)] = eval_instructions.DEFAULT_LANG,
) -> ReplicaDTO:
    """One replica with its recording instructions already assembled.

    The instruction is built here rather than shipped as a template because
    the recording protocol IS a measurement condition: a second copy in the
    SPA would drift, and the drifting copy is the one the recordist reads.
    """
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        line = await eval_lines.resolve(conn, script_id)
        if line is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, detail={"error": "unknown_script_id"}
            )
        templates = await eval_repo.instruction_templates(conn, lang_ui=lang_ui)
        if not templates and lang_ui != eval_instructions.DEFAULT_LANG:
            templates = await eval_repo.instruction_templates(
                conn, lang_ui=eval_instructions.DEFAULT_LANG
            )
        takes = await eval_repo.list_takes(conn)

    by_condition, by_category = eval_instructions.index(templates)
    instruction = eval_instructions.compose(
        condition=line.condition,
        subset=line.subset,
        by_condition=by_condition,
        by_category=by_category,
    )
    return ReplicaDTO(
        id=line.script_id,
        subset=line.subset,
        language=line.language,
        specialty=line.specialty,
        say=line.say,
        transcript=line.transcript,
        source=line.source,
        editable=line.editable,
        dataset=line.dataset,
        paired=line.paired,
        condition=line.condition,
        recording_instructions=instruction.text,
        condition_instructions=instruction.condition_text,
        category_instructions=instruction.category_text,
        conditions_recorded=sorted(
            {r["condition"] for r in takes if r["script_id"] == script_id}
        ),
    )


@router.get("/takes", response_model=EvalTakeListDTO)
async def list_takes(
    claims: Annotated[Claims, Depends(requires("corpus.review", "phrase"))],
) -> EvalTakeListDTO:
    """Every persisted take in the tenant — the whole team's progress, which
    is what lets colleagues split the labelling job across sittings."""
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        rows = await eval_repo.list_takes(conn)
    items = [_take_dto(r) for r in rows]
    return EvalTakeListDTO(
        items=items, total_duration_ms=sum(t.duration_ms for t in items)
    )


async def _journal_attempt(
    state: Any,
    claims: Claims,
    *,
    script_id: str,
    condition: str,
    duration_ms: int,
    device: str | None,
    status_: str,
    reason: str | None,
    expected_condition: str | None = None,
) -> None:
    """Record an attempt that produced no take (§7).

    Its own short transaction, because the caller is on its way to raising a
    422: the attempt is a fact whether or not the upload succeeded, and
    tying it to the request's failed transaction would erase exactly the
    attempts worth counting.
    """
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        await eval_repo.insert_take_attempt(
            conn,
            tenant_id=claims.tid,
            script_id=script_id,
            take_id=None,
            speaker=claims.sub,
            device=device,
            condition=condition,
            duration_ms=duration_ms,
            audio_sha256=None,
            status=status_,
            reason=reason,
            expected_condition=expected_condition,
        )


@router.post("/takes/{script_id}/discard", status_code=status.HTTP_204_NO_CONTENT)
async def discard_take(
    script_id: str,
    body: DiscardTakeRequest,
    claims: Annotated[Claims, Depends(requires("corpus.review", "phrase"))],
) -> None:
    """Log a recording the operator threw away before uploading it.

    The recorder lets you listen back and re-record; before 0092 those
    attempts left no trace, so "this line took six tries and still sounds
    wrong" was knowledge that lived in one person's memory. No audio is
    sent — the bytes of a discarded take are not evidence of anything, and
    uploading them would be storing failed recordings for no reader.
    """
    state = get_state()
    await _check_rate_limit(state, claims)
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        line = await eval_lines.resolve(conn, script_id)
        if line is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, detail={"error": "unknown_script_id"}
            )
        await eval_repo.insert_take_attempt(
            conn,
            tenant_id=claims.tid,
            script_id=script_id,
            take_id=None,
            speaker=claims.sub,
            device=body.device,
            condition=body.condition,
            duration_ms=body.duration_ms,
            audio_sha256=None,
            status="discarded",
            reason=body.reason or "retake",
            expected_condition=line.condition,
        )


@router.put("/takes/{script_id}", response_model=EvalTakeDTO)
async def save_take(
    script_id: str,
    body: SaveTakeRequest,
    claims: Annotated[Claims, Depends(requires("corpus.review", "phrase"))],
) -> EvalTakeDTO:
    """Upload (or replace) the take for one script line. The audio must be
    16 kHz mono PCM16 WAV — the corpus format is an invariant, so the bytes
    are re-checked here rather than trusted from the encoder."""
    state = get_state()
    await _check_rate_limit(state, claims)

    async with tenant_connection(state.app_pool, claims.tid) as conn:
        line = await eval_lines.resolve(conn, script_id)
    if line is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail={"error": "unknown_script_id"}
        )
    if body.say is not None and body.say != line.say:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail={"error": "script_drift"}
        )
    if not body.condition_confirmed:
        # Epic C's acceptance criterion, stated as a refusal: a take whose
        # condition nobody vouched for does not become a recording. Not
        # journalled as an attempt — nothing was attempted, the client
        # simply did not ask the question yet.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "condition_not_confirmed",
                "expected_condition": line.condition,
            },
        )
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        consented = await eval_repo.has_active_consent(conn, speaker_id=claims.sub)
    if not consented:
        # Epic F: a voice is personal data whatever the script says, and the
        # corpus does not acquire one without a live consent on file. The
        # refusal happens BEFORE the bytes are decoded, so unconsented audio
        # never exists server-side even briefly.
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail={"error": "speaker_consent_required", "scope": "corpus_voice"},
        )

    try:
        wav_bytes = base64.b64decode(body.audio_wav_base64, validate=True)
    except (binascii.Error, ValueError):
        await _journal_attempt(
            state, claims, script_id=script_id, condition=body.condition,
            duration_ms=0, device=body.device, status_="rejected", reason="bad_base64",
            expected_condition=line.condition,
        )
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"error": "bad_base64"}
        ) from None
    try:
        info = parse_wav(wav_bytes)
    except WavFormatError as exc:
        await _journal_attempt(
            state, claims, script_id=script_id, condition=body.condition,
            duration_ms=0, device=body.device, status_="rejected", reason=exc.code,
            expected_condition=line.condition,
        )
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"error": exc.code}
        ) from None
    if not MIN_DURATION_MS <= info.duration_ms <= MAX_DURATION_MS:
        await _journal_attempt(
            state, claims, script_id=script_id, condition=body.condition,
            duration_ms=min(info.duration_ms, MAX_DURATION_MS), device=body.device,
            status_="rejected", reason="bad_duration",
            expected_condition=line.condition,
        )
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "bad_duration", "duration_ms": info.duration_ms},
        )

    async with tenant_connection(state.app_pool, claims.tid) as conn:
        row = await eval_repo.upsert_take(
            conn,
            tenant_id=claims.tid,
            script_id=script_id,
            script_version=eval_script.SCRIPT_VERSION,
            recorded_by=claims.sub,
            language=line.language,
            specialty=line.specialty,
            subset=line.subset,
            # The SERVER's text is what gets stored — the scripted-only
            # invariant, enforced at the boundary.
            say=line.say,
            transcript=line.transcript,
            condition=body.condition,
            duration_ms=info.duration_ms,
            sample_rate=info.sample_rate,
            audio_sha256=hashlib.sha256(wav_bytes).hexdigest(),
            audio_wav=wav_bytes,
            condition_confirmed=True,
        )
        # Same transaction as the take: an attempt journalled without the
        # take it produced, or a take with no attempt behind it, would each
        # be a lie the journal exists to prevent.
        await eval_repo.insert_take_attempt(
            conn,
            tenant_id=claims.tid,
            script_id=script_id,
            take_id=row["id"],
            speaker=claims.sub,
            device=body.device,
            condition=body.condition,
            duration_ms=info.duration_ms,
            audio_sha256=row["audio_sha256"],
            status="saved",
            reason=None,
            # An override is legitimate — the studio may only have a phone
            # today. Silently unrecorded is what is not.
            expected_condition=line.condition,
        )

    dto = _take_dto(row)
    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.CORPUS_EVAL_TAKE_SAVED,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="corpus_eval_take",
        target_id=dto.id,
        payload={
            "script_id": script_id,
            "condition": body.condition,
            "expected_condition": line.condition,
            "condition_mismatch": (
                line.condition is not None and line.condition != body.condition
            ),
            "duration_ms": dto.duration_ms,
            "size_bytes": dto.size_bytes,
        },
    )
    return dto


@router.get("/takes/{script_id}/audio")
async def take_audio(
    script_id: str,
    claims: Annotated[Claims, Depends(requires("corpus.review", "phrase"))],
    condition: Annotated[str | None, Query(max_length=40)] = None,
) -> Response:
    """The stored WAV, so the recorder plays back the exact bytes on file
    rather than a checkmark's word for them.

    ``condition`` picks between a paired replica's two recordings; omitting
    it yields the most recent, which is the only sensible answer for a line
    that has one."""
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        row = await eval_repo.get_take_audio(conn, script_id, condition)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="no take for this line")
    return Response(
        content=bytes(row["audio_wav"]),
        media_type="audio/wav",
        headers={
            "Cache-Control": "no-store",
            "X-Audio-Sha256": row["audio_sha256"],
            "X-Condition": row["condition"],
        },
    )


@router.delete("/takes/{script_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_take(
    script_id: str,
    claims: Annotated[Claims, Depends(requires("corpus.review", "phrase"))],
    condition: Annotated[str | None, Query(max_length=40)] = None,
) -> Response:
    """Remove one recording, or every recording of the line when no
    condition is named."""
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        deleted = await eval_repo.delete_take(conn, script_id, condition)
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="no take for this line")
    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.CORPUS_EVAL_TAKE_DELETED,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="corpus_eval_take",
        target_id=None,
        payload={"script_id": script_id, "condition": condition},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class FlagTakeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    condition: Literal["headset", "laptop-mic", "phone-speaker-distance", "noisy"]
    #: False un-flags — the operator listened again and it is fine.
    flagged: bool = True
    note: str | None = Field(default=None, max_length=200)


class RetakeItemDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    script_id: str
    condition: str
    #: Why this take is on the list, stable order: the human's mark first
    #: because it is a judgement, the derived ones after.
    reasons: list[str]
    note: str | None
    duration_ms: int
    recorded_at: datetime


class RetakeQueueDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[RetakeItemDTO]
    #: The "на перезапис: N" counter in the queue panel.
    total: int
    by_reason: dict[str, int]


@router.post("/takes/{script_id}/flag", response_model=EvalTakeDTO)
async def flag_take(
    script_id: str,
    body: FlagTakeRequest,
    claims: Annotated[Claims, Depends(requires("corpus.review", "phrase"))],
) -> EvalTakeDTO:
    """Mark a take as unusable — Epic E's "ручна позначка «брак»".

    The only retake signal that is STORED. Silence, hallucination and
    condition mismatch are all derivable from evidence with a timestamp, so
    they clear themselves when the line is re-recorded; "I listened to this
    and it is unusable" is not derivable from anything, so it is written
    down — and cleared by the next upload, which is the same rule expressed
    the only way it can be for a fact that lives in a person's head.
    """
    state = get_state()
    await _check_rate_limit(state, claims)
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        row = await eval_repo.flag_take(
            conn,
            script_id=script_id,
            condition=body.condition,
            flagged=body.flagged,
            note=body.note,
            flagged_by=claims.sub,
        )
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail={"error": "no_take_for_condition"}
        )
    dto = _take_dto(row)
    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.CORPUS_EVAL_TAKE_FLAGGED,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="corpus_eval_take",
        target_id=dto.id,
        payload={
            "script_id": script_id,
            "condition": body.condition,
            "flagged": body.flagged,
        },
    )
    return dto


@router.get("/retakes", response_model=RetakeQueueDTO)
async def retake_queue(
    claims: Annotated[Claims, Depends(requires("corpus.review", "phrase"))],
) -> RetakeQueueDTO:
    """Everything that needs re-recording, with the reason — Epic E.

    Entirely derived except the manual mark (see ``retake_candidates``), so
    a re-recorded line leaves this list on the next read with nothing to
    clear. That is the acceptance criterion: uk-general-a001 appears here
    while its take is silent, and is gone once it is read again.
    """
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        rows = await eval_repo.retake_candidates(conn)

    items: list[RetakeItemDTO] = []
    by_reason: dict[str, int] = {}
    for row in rows:
        reasons: list[str] = []
        if row["flagged_bad"]:
            reasons.append("manual_bad")
        if row["condition_mismatch"]:
            reasons.append("condition_mismatch")
        run_flags = row["run_flags"]
        if isinstance(run_flags, str):
            run_flags = json.loads(run_flags)
        reasons.extend(str(f) for f in (run_flags or ()))
        for reason in reasons:
            by_reason[reason] = by_reason.get(reason, 0) + 1
        items.append(
            RetakeItemDTO(
                script_id=row["script_id"],
                condition=row["condition"],
                reasons=reasons,
                note=row["flagged_note"],
                duration_ms=int(row["duration_ms"]),
                recorded_at=row["updated_at"],
            )
        )
    return RetakeQueueDTO(items=items, total=len(items), by_reason=by_reason)


@router.get("/export")
async def export_takes(
    claims: Annotated[Claims, Depends(requires("corpus.review", "phrase"))],
    snapshot_id: Annotated[UUID | None, Query()] = None,
) -> Response:
    """The corpus as files: audio.wav + transcript.txt + metadata.json per
    utterance in eval/corpus/v1/ shape, plus a manifest FRAGMENT whose
    per-file SHA-256 lets build_corpus_manifest.py's rebuild be checked
    rather than trusted.

    Without ``snapshot_id`` this is the live set — every take in the tenant
    as it stands right now. With one, it is exactly what that snapshot
    published, and the route REFUSES to build an archive that would differ
    from it: a take deleted since publication (409 ``take_missing``) or
    re-recorded since (409 ``take_drifted``) means the bytes on hand are no
    longer the bytes that were published, and quietly substituting them
    would break the one promise a snapshot makes. Publish a new version.

    Committing the archive into the repo stays the operator step it always
    was — but it now happens once, over everyone's work.
    """
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        version: int | None = None
        if snapshot_id is not None:
            snap = await eval_repo.get_snapshot(conn, snapshot_id)
            if snap is None:
                raise HTTPException(
                    status.HTTP_404_NOT_FOUND, detail={"error": "unknown_snapshot"}
                )
            version = int(snap["version"])
            rows = await eval_repo.fetch_snapshot_export(conn, snapshot_id)
        else:
            rows = await eval_repo.fetch_for_export(conn)
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="no takes to export")

    if snapshot_id is not None:
        missing = [r["script_id"] for r in rows if r["audio_wav"] is None]
        if missing:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={"error": "take_missing", "script_ids": missing},
            )
        drifted = [r["script_id"] for r in rows if r["audio_drifted"]]
        if drifted:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={"error": "take_drifted", "script_ids": drifted},
            )

    utterances = [
        eval_archive.Utterance(
            script_id=r["script_id"],
            language=r["language"],
            specialty=r["specialty"],
            subset=r["subset"],
            transcript=r["transcript"],
            condition=r["condition"],
            duration_ms=int(r["duration_ms"]),
            source=r["source"] or eval_lines.BUILTIN,
            audio=bytes(r["audio_wav"]),
            paired=bool(r["paired"]),
        )
        for r in rows
    ]
    blob, manifest = eval_archive.build_zip(
        utterances,
        snapshot_version=version,
        readme=_unpack_readme(len(utterances), version),
    )

    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.CORPUS_EVAL_EXPORTED,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="corpus_eval_take",
        target_id=snapshot_id,
        payload={
            "utterances": len(manifest["utterances"]),
            "snapshot_version": version,
        },
    )
    name = f"eval-corpus-v{version}.zip" if version else "eval-corpus-takes.zip"
    return Response(
        content=blob,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "no-store",
        },
    )


def _unpack_readme(count: int, version: int | None = None) -> str:
    what = (
        f"published snapshot v{version}"
        if version is not None
        else "the tenant's current takes"
    )
    return "\n".join(
        [
            "Eval corpus takes",
            "=================",
            "",
            f"{count} utterance(s) from {what}, recorded through the console",
            "eval recorder and stored server-side (migrations 0089/0091).",
            "16 kHz mono PCM 16-bit, per eval/corpus/v1/README.md.",
            "",
            "To install:",
            "",
            "  1. Unzip into medical-dictation-backend/eval/corpus/v1/",
            "     (directories already match the expected layout, including",
            "      subsets/<subset>/<utterance_id>/).",
            "  2. Verify the digests in manifest-fragment.json against the files.",
            "  3. Rebuild the corpus manifest:",
            "       python scripts/eval/build_corpus_manifest.py",
            "  4. Run the PII sweep before committing:",
            "       python scripts/eval/check_corpus_pii.py",
            "",
            "manifest-fragment.json is NOT the corpus manifest. It lists only the",
            "takes in this archive, with a SHA-256 per file taken over exactly",
            "these bytes, so step 3 can be checked rather than trusted.",
            "",
            "dictation_source is 'authored_by_clinician' for every take: the",
            "recorder cannot produce anything else — the upload route refuses",
            "any line that is not in the server's script (vendored or authored",
            "in this tenant). Lines whose text was written down AFTER the",
            "recording carry capture='adhoc' in metadata.json; for the rest the",
            "words existed before the microphone was opened.",
            "",
            "The WER these utterances produce is also computable without this",
            "archive: publish a snapshot and score it from the console",
            "(POST /corpus/eval/runs). The archive is for the git-committed",
            "corpus and the nightly release gate, which stay the rig's job.",
            "",
        ]
    )
