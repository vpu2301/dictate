"""/corpus/eval — authoring, publishing and scoring (migration 0091).

0089 made takes survive the tab. This module makes them produce the thing
they were recorded for:

  POST   /corpus/eval/script          add a line, with the same category and
  PATCH  /corpus/eval/script/{id}     labels the vendored ones carry
  DELETE /corpus/eval/script/{id}
  POST   /corpus/eval/adhoc           record first, write the gold text after

  POST   /corpus/eval/publish         freeze the current takes as an
  GET    /corpus/eval/snapshots       immutable, PII-swept snapshot
  GET    /corpus/eval/snapshots/{id}

  POST   /corpus/eval/runs            score a snapshot through asr-service
  POST   /corpus/eval/runs/{id}/advance
  GET    /corpus/eval/runs
  GET    /corpus/eval/runs/{id}

WHY A SNAPSHOT SITS BETWEEN RECORDING AND SCORING. A WER is a claim about a
specific set of audio and a specific set of gold texts. Scoring the live
takes would mean a colleague re-recording one line mid-run silently changes
what the number refers to, and two runs a week apart would be
incomparable for reasons nothing recorded. Publishing freezes the set,
hashes every file, and gives the number something to be about.

WHY THE RUN IS PUMPED BY THE CLIENT. Transcription takes minutes; the run
must outlive one HTTP request. The two usual answers are a background task
holding the caller's bearer, or a service credential — the first stores a
token, the second creates a credential that can transcribe anything in any
tenant. Instead ``/advance`` is a resumable step: it polls what is
in-flight, submits what fits in the concurrency budget, writes what
finished, and returns. The console calls it every couple of seconds. A
closed tab leaves a run that resumes exactly where it stopped, with no
token stored anywhere and no privilege that outlives the request.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from audit import Severity
from auth import Claims
from db import tenant_connection

from .. import (
    audit_kinds,
    eval_archive,
    eval_flags,
    eval_goldlint,
    eval_import,
    eval_lines,
    eval_normalize,
    eval_pii,
    eval_register,
    eval_stats,
    eval_wer,
)
from .. import eval_repository as eval_repo
from ..config import settings
from ..deps import get_state, requires
from ..eval_wav import WavFormatError, parse_wav
from ..integrations.asr_client import AsrClientError
from .corpus_eval import (
    MAX_AUDIO_B64_CHARS,
    MAX_DURATION_MS,
    MIN_DURATION_MS,
    EvalTakeDTO,
)
from .phrases import _check_rate_limit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/corpus/eval", tags=["corpus"])

# asr-service's batch edge takes uk|en only (its Form pattern). German lines
# may be authored and recorded — the corpus is the corpus — they just cannot
# be scored yet, and the run says so per utterance instead of failing.
SCORABLE_LANGUAGES = ("uk", "en")


# ══ DTOs ═══════════════════════════════════════════════════════════════


class LineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    language: str
    specialty: str = Field(min_length=1, max_length=eval_lines.MAX_SPECIALTY)
    # None = a baseline line. Otherwise one of the six adversarial subsets.
    subset: str | None = None
    say: str = Field(min_length=1, max_length=eval_lines.MAX_TEXT)
    # The gold text, when it differs from what is spoken ("140/90 мм рт.
    # ст." for "сто сорок на дев'яносто"). Omitted means they coincide —
    # which is the honest default, not a shortcut: a line with no
    # normalisation to test has nothing to write here.
    transcript: str | None = Field(default=None, max_length=eval_lines.MAX_TEXT)
    condition: str | None = None
    # Which half of the corpus this line joins (0092). New lines default to
    # dev: the frozen holdout does not grow, or it is not a holdout.
    dataset: Literal["dev", "test"] = "dev"
    # Part of the paired design (0095, Epic D): recorded in BOTH conditions
    # so headset and phone can be compared on the SAME text. Requires a
    # condition — "and also the other one" needs a first one to be other
    # than.
    paired: bool = False


class GoldLintFindingDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    #: One of eval_goldlint.CODES; the console maps it to Ukrainian text.
    code: str
    #: The offending fragment, so the console can point at it.
    detail: str


class GoldLintDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    findings: list[GoldLintFindingDTO] = Field(default_factory=list)
    #: The verified spoken rewrite, or null when none can be offered safely.
    suggestion: str | None = None


class LineDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    script_id: str
    language: str
    specialty: str
    subset: str | None
    say: str
    transcript: str
    condition: str | None
    source: str
    dataset: str
    paired: bool = False
    created_by: UUID
    created_at: datetime
    updated_at: datetime
    #: Epic B style verdict on this line's gold, computed on every read. The
    #: console shows it at the point of authoring, which is the only moment
    #: fixing it is free.
    lint: GoldLintDTO = Field(default_factory=GoldLintDTO)


class AdhocRequest(LineRequest):
    condition: Literal["headset", "laptop-mic", "phone-speaker-distance", "noisy"]
    audio_wav_base64: str = Field(min_length=64, max_length=MAX_AUDIO_B64_CHARS)
    # The attestation. Ad-hoc capture is the one path where the words are
    # not known before the microphone opens, so the scripted-only invariant
    # cannot do the work it does everywhere else. The PII sweep still runs,
    # but no regex separates "пацієнт скаржиться" from "пацієнт Шевченко
    # скаржиться" — a human has to say this recording contains no real
    # patient, and that statement is stored in the audit trail with their
    # name on it.
    no_patient_data: bool = False


class AdhocResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    line: LineDTO
    take: EvalTakeDTO


class SnapshotDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    version: int
    utterance_count: int
    total_duration_ms: int
    manifest_sha256: str
    published_by: UUID
    created_at: datetime


class SnapshotItemDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    script_id: str
    language: str
    specialty: str
    subset: str | None
    transcript: str
    condition: str
    duration_ms: int
    audio_sha256: str
    source: str
    dataset: str


class SnapshotDetailDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    snapshot: SnapshotDTO
    items: list[SnapshotItemDTO]
    # How many utterances each set contributes — what the console needs to
    # offer "score dev" and "score test" as separate, honest buttons.
    dataset_counts: dict[str, int] = Field(default_factory=dict)


class SnapshotListDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[SnapshotDTO]


class StartRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    snapshot_id: UUID
    # Defaults to the holdout, which is what a run meant before 0092 split
    # the corpus — an old client keeps measuring exactly what it measured.
    # The console asks explicitly, because a dev number and a test number
    # answer different questions (§7 "правила чесності").
    dataset: Literal["dev", "test"] = "test"


class RunItemDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    script_id: str
    status: str
    hypothesis: str | None
    wer: float | None
    cer: float | None
    ref_words: int | None
    error: str | None
    # ── corpus-v2 ──────────────────────────────────────────────────────
    wer_norm: float | None = None
    cer_norm: float | None = None
    dose_tokens: int | None = None
    dose_exact: bool | None = None
    #: Non-empty means this utterance is quarantined out of the averages.
    flags: list[str] = Field(default_factory=list)
    speech_ms: int | None = None
    updated_at: datetime
    #: True when this line's gold transcript was rewritten AFTER the run
    #: started (Epic B). The score is unchanged and still real — it simply
    #: describes a comparison against a reference that no longer exists.
    gold_revised: bool = False


class RunDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    snapshot_id: UUID
    snapshot_version: int
    status: str
    model: str
    started_by: UUID
    started_at: datetime
    finished_at: datetime | None
    summary: dict[str, Any] | None
    counts: dict[str, int]
    # ── the measurement conditions (§1.3.5) ────────────────────────────
    dataset: str = "test"
    normalizer_version: str = "v0"
    corpus_sha256: str | None = None
    engine: dict[str, Any] = Field(default_factory=dict)
    bootstrap_seed: int = eval_stats.DEFAULT_SEED


class RunDetailDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run: RunDTO
    items: list[RunItemDTO]


class RunListDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[RunDTO]


# ══ shared guards ══════════════════════════════════════════════════════


async def _reject_pii(
    state: Any, claims: Claims, *, texts: dict[str, str], target_kind: str
) -> None:
    """The corpus PII sweep at a write boundary.

    Names the pattern CLASS and never the match — the finding travels into
    a 422 body, an audit payload and a metric label, and the matched text
    must not appear in any of them.
    """
    findings: dict[str, list[str]] = {}
    for field, text in texts.items():
        hits = eval_pii.sweep(text)
        if hits:
            findings[field] = hits
    if not findings:
        return
    for hits in findings.values():
        for pattern in hits:
            state.pii_rejections_metric.add(1, {"pattern": pattern})
    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.PHRASE_WRITE_REJECTED_PII,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind=target_kind,
        target_id=None,
        payload={
            "findings": findings,
            "text_lengths": {k: len(v) for k, v in texts.items()},
        },
        severity=Severity.SEC,
    )
    raise HTTPException(
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={
            "error": "pii_detected",
            "findings": findings,
            "message": "містить дані, схожі на персональні — не збережено",
        },
    )


def _validate_or_422(body: LineRequest) -> None:
    errors = eval_lines.validate(
        language=body.language,
        specialty=body.specialty,
        subset=body.subset,
        say=body.say,
        transcript=body.transcript,
        condition=body.condition,
        dataset=body.dataset,
        paired=body.paired,
    )
    if errors:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "invalid_line",
                "fields": [{"field": e.field, "code": e.code} for e in errors],
            },
        )


def _lint_dto(result: eval_goldlint.LintResult) -> GoldLintDTO:
    return GoldLintDTO(
        findings=[
            GoldLintFindingDTO(code=f.code, detail=f.detail) for f in result.findings
        ],
        suggestion=result.suggestion,
    )


def _line_dto(rec: asyncpg.Record) -> LineDTO:
    return LineDTO(
        id=rec["id"],
        script_id=rec["script_id"],
        language=rec["language"],
        specialty=rec["specialty"],
        subset=rec["subset"],
        say=rec["say"],
        transcript=rec["transcript"],
        condition=rec["condition"],
        source=rec["source"],
        dataset=rec["dataset"],
        paired=rec["paired"],
        created_by=rec["created_by"],
        created_at=rec["created_at"],
        updated_at=rec["updated_at"],
        lint=_lint_dto(
            eval_goldlint.lint_gold(
                say=rec["say"],
                transcript=rec["transcript"],
                language=rec["language"],
            )
        ),
    )


async def _insert_line(
    conn: asyncpg.Connection,
    *,
    claims: Claims,
    body: LineRequest,
    source: str,
) -> asyncpg.Record:
    """Allocate an id and insert, retrying past a lost race.

    Two colleagues adding a cardiology line in the same second would
    otherwise both compute ``uk-cardiology-a007`` and one would get a
    constraint violation as a 500. The insert answers None on conflict, so
    the loop simply asks for the next free id.
    """
    for _ in range(5):
        script_id = await eval_lines.allocate_script_id(
            conn, language=body.language, specialty=body.specialty
        )
        rec = await eval_repo.insert_script_item(
            conn,
            tenant_id=claims.tid,
            script_id=script_id,
            language=body.language,
            specialty=body.specialty,
            subset=body.subset,
            say=body.say.strip(),
            transcript=(body.transcript or body.say).strip(),
            condition=body.condition,
            source=source,
            created_by=claims.sub,
            dataset=body.dataset,
            paired=body.paired,
        )
        if rec is not None:
            return rec
    raise HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"error": "script_id_allocation_failed"},
    )


# ══ authoring ══════════════════════════════════════════════════════════


@router.post("/script", response_model=LineDTO, status_code=status.HTTP_201_CREATED)
async def create_line(
    body: LineRequest,
    claims: Annotated[Claims, Depends(requires("corpus.contribute", "phrase"))],
) -> LineDTO:
    """Add a line to the recording script.

    The corpus grew by editing a Python module before this; that made
    "record the cases our clinicians actually struggle with" a developer
    task, which is the reason the corpus stayed at 34 lines. The line lands
    in the tenant's own half of the script — the vendored spine is shared
    and stays untouched — and is recordable immediately.
    """
    state = get_state()
    await _check_rate_limit(state, claims)
    _validate_or_422(body)
    await _reject_pii(
        state,
        claims,
        texts={"say": body.say, "transcript": body.transcript or body.say},
        target_kind="corpus_eval_line",
    )

    async with tenant_connection(state.app_pool, claims.tid) as conn:
        rec = await _insert_line(conn, claims=claims, body=body, source=eval_lines.AUTHORED)
    dto = _line_dto(rec)
    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.CORPUS_EVAL_LINE_ADDED,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="corpus_eval_line",
        target_id=dto.id,
        payload={
            "script_id": dto.script_id,
            "language": dto.language,
            "specialty": dto.specialty,
            "subset": dto.subset,
            "source": dto.source,
        },
    )
    return dto


@router.patch("/script/{script_id}", response_model=LineDTO)
async def update_line(
    script_id: str,
    body: LineRequest,
    claims: Annotated[Claims, Depends(requires("corpus.contribute", "phrase"))],
) -> LineDTO:
    """Edit an authored line. Vendored lines are refused (409): they are the
    shared spine, and a take recorded against one in any tenant was recorded
    against THAT text.

    Editing a line whose take already exists does not touch the take. The
    take carries its own snapshot of the words that were actually read
    (migration 0089), so the audio never starts claiming to be a reading of
    something else — but the two now disagree, and re-recording is the only
    way to make the line true again. The console says so at the point of
    edit; nothing here can enforce it.
    """
    state = get_state()
    _validate_or_422(body)
    await _reject_pii(
        state,
        claims,
        texts={"say": body.say, "transcript": body.transcript or body.say},
        target_kind="corpus_eval_line",
    )

    async with tenant_connection(state.app_pool, claims.tid) as conn:
        if script_id in eval_lines.ROW_BY_ID:
            raise HTTPException(
                status.HTTP_409_CONFLICT, detail={"error": "builtin_line_immutable"}
            )
        rec = await eval_repo.update_script_item(
            conn,
            script_id=script_id,
            language=body.language,
            specialty=body.specialty,
            subset=body.subset,
            say=body.say.strip(),
            transcript=(body.transcript or body.say).strip(),
            condition=body.condition,
            dataset=body.dataset,
            paired=body.paired,
        )
    if rec is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail={"error": "unknown_script_id"}
        )
    dto = _line_dto(rec)
    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.CORPUS_EVAL_LINE_UPDATED,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="corpus_eval_line",
        target_id=dto.id,
        payload={"script_id": dto.script_id, "subset": dto.subset},
    )
    return dto


@router.delete("/script/{script_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_line(
    script_id: str,
    claims: Annotated[Claims, Depends(requires("corpus.contribute", "phrase"))],
) -> None:
    """Remove an authored line AND its take — a line with no text is not a
    recordable task, and leaving orphan audio behind would leave a take the
    recorder cannot show, delete or explain.

    Published snapshots are unaffected: they hold their own copy of the
    text, and the audio they reference is exactly what the export refuses to
    substitute when it goes missing (409 ``take_missing``). Deleting a line
    can therefore make an old snapshot unexportable — it can never make one
    quietly wrong.
    """
    state = get_state()
    if script_id in eval_lines.ROW_BY_ID:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail={"error": "builtin_line_immutable"}
        )
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        deleted = await eval_repo.delete_script_item(conn, script_id)
        if deleted:
            await eval_repo.delete_take(conn, script_id)
    if not deleted:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail={"error": "unknown_script_id"}
        )
    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.CORPUS_EVAL_LINE_DELETED,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="corpus_eval_line",
        target_id=None,
        payload={"script_id": script_id},
    )


@router.post("/adhoc", response_model=AdhocResponse, status_code=status.HTTP_201_CREATED)
async def save_adhoc(
    body: AdhocRequest,
    claims: Annotated[Claims, Depends(requires("corpus.contribute", "phrase"))],
) -> AdhocResponse:
    """Record first, write the gold text after — one call, one transaction.

    This is the path for the utterance nobody thought to script: the phrasing
    a clinician actually uses, captured while it is still in the room. It
    creates the line and its take together, because a line authored from a
    recording and never attached to it would be a script entry nobody can
    account for.

    THREE THINGS GUARD IT, and they are not interchangeable:
      · the attestation (``no_patient_data``), refused when false — the only
        control over names, which no regex catches;
      · the PII sweep over the typed text, which catches identifiers a tired
        human misses;
      · ``capture: adhoc`` in the corpus metadata, so a reader of the corpus
        knows the gold text was reconstructed from speech rather than read
        from a page.
    """
    state = get_state()
    await _check_rate_limit(state, claims)
    _validate_or_422(body)
    if not body.no_patient_data:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "attestation_required",
                "message": (
                    "ad-hoc capture requires an explicit no-patient-data "
                    "attestation"
                ),
            },
        )
    await _reject_pii(
        state,
        claims,
        texts={"say": body.say, "transcript": body.transcript or body.say},
        target_kind="corpus_eval_line",
    )

    try:
        wav_bytes = base64.b64decode(body.audio_wav_base64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"error": "bad_base64"}
        ) from None
    try:
        info = parse_wav(wav_bytes)
    except WavFormatError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"error": exc.code}
        ) from None
    if not MIN_DURATION_MS <= info.duration_ms <= MAX_DURATION_MS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "bad_duration", "duration_ms": info.duration_ms},
        )

    async with tenant_connection(state.app_pool, claims.tid) as conn:
        line_rec = await _insert_line(
            conn, claims=claims, body=body, source=eval_lines.ADHOC
        )
        take_rec = await eval_repo.upsert_take(
            conn,
            tenant_id=claims.tid,
            script_id=line_rec["script_id"],
            script_version=eval_lines.SCRIPT_VERSION,
            recorded_by=claims.sub,
            language=line_rec["language"],
            specialty=line_rec["specialty"],
            subset=line_rec["subset"],
            say=line_rec["say"],
            transcript=line_rec["transcript"],
            condition=body.condition,
            duration_ms=info.duration_ms,
            sample_rate=info.sample_rate,
            audio_sha256=hashlib.sha256(wav_bytes).hexdigest(),
            audio_wav=wav_bytes,
        )

    line = _line_dto(line_rec)
    take = EvalTakeDTO(
        id=take_rec["id"],
        script_id=take_rec["script_id"],
        script_version=take_rec["script_version"],
        recorded_by=take_rec["recorded_by"],
        language=take_rec["language"],
        specialty=take_rec["specialty"],
        subset=take_rec["subset"],
        condition=take_rec["condition"],
        duration_ms=int(take_rec["duration_ms"]),
        audio_sha256=take_rec["audio_sha256"],
        size_bytes=int(take_rec["size_bytes"]),
        created_at=take_rec["created_at"],
        updated_at=take_rec["updated_at"],
    )
    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.CORPUS_EVAL_ADHOC_CAPTURED,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="corpus_eval_line",
        target_id=line.id,
        payload={
            "script_id": line.script_id,
            "language": line.language,
            "specialty": line.specialty,
            "subset": line.subset,
            "duration_ms": take.duration_ms,
            # The attestation is the record that matters here: it is the
            # only reason this audio was allowed to exist unscripted.
            "no_patient_data_attested": True,
        },
    )
    return AdhocResponse(line=line, take=take)


# ══ publishing ═════════════════════════════════════════════════════════


def _snapshot_dto(rec: asyncpg.Record) -> SnapshotDTO:
    return SnapshotDTO(
        id=rec["id"],
        version=int(rec["version"]),
        utterance_count=int(rec["utterance_count"]),
        total_duration_ms=int(rec["total_duration_ms"]),
        manifest_sha256=rec["manifest_sha256"],
        published_by=rec["published_by"],
        created_at=rec["created_at"],
    )


@router.post("/publish", response_model=SnapshotDTO, status_code=status.HTTP_201_CREATED)
async def publish_snapshot(
    claims: Annotated[Claims, Depends(requires("corpus.review", "phrase"))],
) -> SnapshotDTO:
    """Freeze every current take as an immutable, numbered snapshot.

    The sweep runs over the WHOLE set, not just the lines added since the
    last publication: patterns get added, and a snapshot's claim is that
    THIS set is clean by today's rules. A finding refuses the publication
    outright — there is no override, because the next step feeds these files
    to a git repository and a nightly gate that will refuse them anyway,
    louder and later.
    """
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        all_rows = await eval_repo.fetch_for_publish(conn)
        # Epic F: a speaker who withdrew consent leaves FUTURE measurements.
        # Published snapshots are untouched — the basis that existed when
        # they were frozen is not unmade by a later withdrawal — and the
        # count is reported rather than silently applied.
        rows = [r for r in all_rows if not r["excluded_by_consent"]]
        excluded = len(all_rows) - len(rows)
        if not rows:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                detail={"error": "no_takes", "excluded_by_consent": excluded},
            )

        findings: dict[str, list[str]] = {}
        for r in rows:
            hits = sorted(set(eval_pii.sweep(r["transcript"]) + eval_pii.sweep(r["say"])))
            if hits:
                findings[r["script_id"]] = hits
        if findings:
            for hits in findings.values():
                for pattern in hits:
                    state.pii_rejections_metric.add(1, {"pattern": pattern})
            await state.audit_writer.write_event(
                tenant_id=claims.tid,
                kind=audit_kinds.PHRASE_WRITE_REJECTED_PII,
                actor_sub=claims.sub,
                actor_role=(claims.roles[0] if claims.roles else None),
                target_kind="corpus_eval_snapshot",
                target_id=None,
                payload={"findings": findings, "utterances": len(rows)},
                severity=Severity.SEC,
            )
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "pii_detected", "findings": findings},
            )

        version = await eval_repo.next_snapshot_version(conn)
        entries = []
        total_ms = 0
        for r in rows:
            rendered = eval_archive.render(
                eval_archive.Utterance(
                    script_id=r["script_id"],
                    language=r["language"],
                    specialty=r["specialty"],
                    subset=r["subset"],
                    transcript=r["transcript"],
                    condition=r["condition"],
                    duration_ms=int(r["duration_ms"]),
                    source=r["source"],
                    audio=bytes(r["audio_wav"]),
                    paired=r["paired"],
                )
            )
            entries.append(rendered.manifest_entry)
            total_ms += int(r["duration_ms"])
        manifest = eval_archive.build_manifest(entries, snapshot_version=version)
        manifest_sha = hashlib.sha256(eval_archive.manifest_bytes(manifest)).hexdigest()

        snap = await eval_repo.insert_snapshot(
            conn,
            tenant_id=claims.tid,
            version=version,
            utterance_count=len(rows),
            total_duration_ms=total_ms,
            manifest=json.dumps(manifest, ensure_ascii=False),
            manifest_sha256=manifest_sha,
            published_by=claims.sub,
        )
        await eval_repo.insert_snapshot_items(
            conn,
            tenant_id=claims.tid,
            snapshot_id=snap["id"],
            items=[
                {
                    "script_id": r["script_id"],
                    "language": r["language"],
                    "specialty": r["specialty"],
                    "subset": r["subset"],
                    "transcript": r["transcript"],
                    "condition": r["condition"],
                    "duration_ms": int(r["duration_ms"]),
                    "audio_sha256": r["audio_sha256"],
                    "take_id": r["take_id"],
                    "source": r["source"],
                    "dataset": r["dataset"],
                    "paired": r["paired"],
                }
                for r in rows
            ],
        )
        # Epic F: the snapshot registers itself. A register somebody has to
        # remember to update is out of date the first busy week, and the
        # digest it needs was computed for integrity a few lines up — one
        # digest per artefact, never two.
        await eval_repo.upsert_registry_entry(
            conn,
            tenant_id=claims.tid,
            **eval_register.snapshot_entry(
                version=version,
                manifest_sha256=manifest_sha,
                snapshot_id=snap["id"],
                utterances=len(rows),
                speakers=sorted({r["recorded_by"] for r in rows}),
            ),
        )

    dto = _snapshot_dto(snap)
    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.CORPUS_EVAL_PUBLISHED,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="corpus_eval_snapshot",
        target_id=dto.id,
        payload={
            "version": dto.version,
            "utterances": dto.utterance_count,
            "manifest_sha256": dto.manifest_sha256,
            "excluded_by_consent": excluded,
        },
    )
    return dto


@router.get("/snapshots", response_model=SnapshotListDTO)
async def list_snapshots(
    claims: Annotated[Claims, Depends(requires("corpus.review", "phrase"))],
) -> SnapshotListDTO:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        rows = await eval_repo.list_snapshots(conn)
    return SnapshotListDTO(items=[_snapshot_dto(r) for r in rows])


@router.get("/snapshots/{snapshot_id}", response_model=SnapshotDetailDTO)
async def snapshot_detail(
    snapshot_id: UUID,
    claims: Annotated[Claims, Depends(requires("corpus.review", "phrase"))],
) -> SnapshotDetailDTO:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        snap = await eval_repo.get_snapshot(conn, snapshot_id)
        if snap is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, detail={"error": "unknown_snapshot"}
            )
        items = await eval_repo.list_snapshot_items(conn, snapshot_id)
        counts = await eval_repo.snapshot_dataset_counts(conn, snapshot_id)
    return SnapshotDetailDTO(
        snapshot=_snapshot_dto(snap),
        items=[
            SnapshotItemDTO(
                script_id=i["script_id"],
                language=i["language"],
                specialty=i["specialty"],
                subset=i["subset"],
                transcript=i["transcript"],
                condition=i["condition"],
                duration_ms=int(i["duration_ms"]),
                audio_sha256=i["audio_sha256"],
                source=i["source"],
                dataset=i["dataset"],
            )
            for i in items
        ],
        dataset_counts=counts,
    )


# ══ scoring ════════════════════════════════════════════════════════════


def _jsonb(value: Any, fallback: Any) -> Any:
    """asyncpg hands jsonb back as text on a connection with no codec set."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return fallback
    return fallback if value is None else value


def _run_dto(rec: asyncpg.Record, counts: dict[str, int]) -> RunDTO:
    summary = rec["summary"]
    if isinstance(summary, str):
        summary = json.loads(summary)
    return RunDTO(
        id=rec["id"],
        snapshot_id=rec["snapshot_id"],
        snapshot_version=int(rec["snapshot_version"]),
        status=rec["status"],
        model=rec["model"],
        started_by=rec["started_by"],
        started_at=rec["started_at"],
        finished_at=rec["finished_at"],
        summary=summary,
        counts=counts,
        dataset=rec["dataset"],
        normalizer_version=rec["normalizer_version"],
        corpus_sha256=rec["corpus_sha256"],
        engine=_jsonb(rec["engine"], {}),
        bootstrap_seed=int(rec["bootstrap_seed"]),
    )


def _run_item_dto(rec: asyncpg.Record, *, gold_revised: bool = False) -> RunItemDTO:
    return RunItemDTO(
        gold_revised=gold_revised,
        script_id=rec["script_id"],
        status=rec["status"],
        hypothesis=rec["hypothesis"],
        wer=rec["wer"],
        cer=rec["cer"],
        ref_words=rec["ref_words"],
        error=rec["error"],
        wer_norm=rec["wer_norm"],
        cer_norm=rec["cer_norm"],
        dose_tokens=rec["dose_tokens"],
        dose_exact=rec["dose_exact"],
        flags=list(_jsonb(rec["flags"], [])),
        speech_ms=rec["speech_ms"],
        updated_at=rec["updated_at"],
    )


def _score(reference_row: asyncpg.Record, tr: Any) -> dict[str, Any]:
    """Everything one completed utterance yields, computed in one place.

    Raw and normalised WER, raw and normalised CER, the dose verdict and the
    hallucination flags all describe the same comparison; splitting them
    across call sites is how a run ends up with a normalised score computed
    against a differently-tokenised reference.

    The flags run on the RAW rate on purpose (see eval_flags): normalisation
    can only lower a score, so judging quarantine on the normalised number
    would let a hypothesis about nothing slip under the threshold.
    """
    reference = str(reference_row["transcript"])
    language = str(reference_row["language"])
    hypothesis = tr.text

    wer_value, ref_words = eval_wer.wer(reference, hypothesis)
    cer_value, _ = eval_wer.cer(reference, hypothesis)

    ref_norm = eval_normalize.normalize(reference, language)
    hyp_norm = eval_normalize.normalize(hypothesis, language)
    wer_norm, ref_words_norm = eval_wer.wer(ref_norm, hyp_norm)
    cer_norm, _ = eval_wer.cer(ref_norm, hyp_norm)

    ref_doses = eval_normalize.numeric_signature(reference, language)
    hyp_doses = eval_normalize.numeric_signature(hypothesis, language)

    duration_ms = reference_row["duration_ms"]
    return {
        "script_id": str(reference_row["script_id"]),
        "condition": str(reference_row["condition"]),
        "hypothesis": hypothesis,
        "wer": wer_value,
        "cer": cer_value,
        "ref_words": ref_words,
        "wer_norm": wer_norm,
        "cer_norm": cer_norm,
        "ref_words_norm": ref_words_norm,
        "ref_chars_norm": len(ref_norm),
        "dose_tokens": len(ref_doses),
        # Exact equality, sequence included: "40 mg twice" and "twice 40 mg"
        # are not the same prescription.
        "dose_exact": ref_doses == hyp_doses,
        "speech_ms": tr.speech_ms,
        "flags": eval_flags.detect(
            wer=wer_value,
            hypothesis=hypothesis,
            speech_ms=tr.speech_ms,
            duration_ms=int(duration_ms) if duration_ms is not None else None,
        ),
    }


def _note_engine(engine: dict[str, Any], tr: Any, reference_row: asyncpg.Record) -> None:
    """Accumulate the decode conditions across a run's utterances.

    Language hints are a SET, not a value: a mixed-language snapshot is
    scored with a different hint per utterance, and recording only the last
    one would describe the run as monolingual when it was not.

    `temperature` is recorded as null with a reason. No ASR surface here
    exposes it; writing a plausible default into a field whose whole purpose
    is reproducibility would make the record worse than the gap.
    """
    if tr.beam_size is not None:
        engine["beam_size"] = tr.beam_size
    if tr.nlp_pipeline_version:
        engine["nlp_pipeline_version"] = tr.nlp_pipeline_version
    engine["nlp_applied"] = bool(tr.nlp_applied)
    engine.setdefault("temperature", None)
    engine.setdefault("temperature_source", "not_reported_by_asr_service")
    hints = set(engine.get("language_hints") or [])
    hints.add(str(reference_row["language"]))
    engine["language_hints"] = sorted(hints)
    if tr.prompt_id:
        prompts = set(engine.get("prompt_ids") or [])
        prompts.add(tr.prompt_id)
        engine["prompt_ids"] = sorted(prompts)


@router.post("/runs", response_model=RunDetailDTO, status_code=status.HTTP_201_CREATED)
async def start_run(
    body: StartRunRequest,
    claims: Annotated[Claims, Depends(requires("corpus.review", "phrase"))],
) -> RunDetailDTO:
    """Open a scoring run over ONE SET of a published snapshot.

    Every utterance is listed up front, so progress is a count against a
    known total from the first tick. Nothing is transcribed here — the run
    is created empty-handed and moves only when ``/advance`` is called.

    The run records the conditions it will be measured under before it
    measures anything (§1.3.5): which set, which normalisation rules, which
    corpus digest, which bootstrap seed. The engine half is filled in from
    what asr-service reports, because that is the half nobody here gets to
    assert.
    """
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        snap = await eval_repo.get_snapshot(conn, body.snapshot_id)
        if snap is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, detail={"error": "unknown_snapshot"}
            )
        all_items = await eval_repo.list_snapshot_items(conn, body.snapshot_id)
        if not all_items:
            raise HTTPException(
                status.HTTP_409_CONFLICT, detail={"error": "empty_snapshot"}
            )
        items = [i for i in all_items if i["dataset"] == body.dataset]
        if not items:
            # A snapshot published before the corpus was split holds only
            # 'test' rows; asking it for dev is a real, answerable "there is
            # nothing to score here yet" rather than a run that completes
            # with an empty summary.
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "error": "empty_dataset",
                    "dataset": body.dataset,
                    "available": sorted({str(i["dataset"]) for i in all_items}),
                },
            )
        run = await eval_repo.insert_run(
            conn,
            tenant_id=claims.tid,
            snapshot_id=body.snapshot_id,
            # Corrected from what asr-service reports on the first result.
            model="unknown",
            started_by=claims.sub,
            dataset=body.dataset,
            normalizer_version=eval_normalize.VERSION,
            corpus_sha256=snap["manifest_sha256"],
            bootstrap_seed=eval_stats.DEFAULT_SEED,
        )
        await eval_repo.insert_run_items(
            conn,
            tenant_id=claims.tid,
            run_id=run["id"],
            items=[
                (
                    i["script_id"],
                    i["condition"],
                    "pending" if i["language"] in SCORABLE_LANGUAGES else "skipped",
                    None
                    if i["language"] in SCORABLE_LANGUAGES
                    else "language_unsupported",
                )
                for i in items
            ],
        )
        counts = await eval_repo.count_by_status(conn, run["id"])
        run_row = await eval_repo.get_run(conn, run["id"])
        item_rows = await eval_repo.list_run_items(conn, run["id"])

    assert run_row is not None
    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.CORPUS_EVAL_RUN_STARTED,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="corpus_eval_run",
        target_id=run["id"],
        payload={
            "snapshot_version": int(snap["version"]),
            "utterances": len(items),
            "dataset": body.dataset,
            "normalizer_version": eval_normalize.VERSION,
            "corpus_sha256": snap["manifest_sha256"],
        },
    )
    return RunDetailDTO(
        run=_run_dto(run_row, counts),
        items=[_run_item_dto(r) for r in item_rows],
    )


@router.post("/runs/{run_id}/advance", response_model=RunDetailDTO)
async def advance_run(
    run_id: UUID,
    claims: Annotated[Claims, Depends(requires("corpus.review", "phrase"))],
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> RunDetailDTO:
    """One step of the run: collect what finished, submit what fits.

    Idempotent and safe to call at any cadence — calling it twice a second
    does the same thing as calling it once, because progress lives in the
    rows, not in the request.

    NO DATABASE TRANSACTION IS HELD ACROSS AN HTTP CALL. Each phase is a
    short transaction with the network work in between, so a slow or dead
    asr-service parks a pooled connection for exactly as long as it takes
    to write a row.
    """
    state = get_state()
    asr = state.asr_client

    async with tenant_connection(state.app_pool, claims.tid) as conn:
        run = await eval_repo.get_run(conn, run_id)
        if run is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, detail={"error": "unknown_run"}
            )
        if run["status"] != "running":
            counts = await eval_repo.count_by_status(conn, run_id)
            items = await eval_repo.list_run_items(conn, run_id)
            return RunDetailDTO(
                run=_run_dto(run, counts), items=[_run_item_dto(r) for r in items]
            )
        await eval_repo.reset_stale_claims(
            conn, run_id=run_id, older_than_seconds=settings.eval_claim_stale_seconds
        )
        snapshot_id = run["snapshot_id"]
        # Keyed by (script_id, CONDITION) since 0095: a paired replica has two
        # recordings under one id, and a dict keyed on the id alone would
        # score one of them twice and the other never.
        references = {
            (i["script_id"], i["condition"]): i
            for i in await eval_repo.list_snapshot_items(conn, snapshot_id)
        }
        in_flight = await eval_repo.in_flight_items(
            conn, run_id=run_id, limit=settings.eval_max_in_flight * 4
        )

    # ── phase 1: poll what is transcribing ────────────────────────────
    scored: list[dict[str, Any]] = []
    failed: list[tuple[str, str, str]] = []
    model_seen: str | None = None
    engine_seen: dict[str, Any] = {}
    still_running = 0
    for item in in_flight:
        key = (item["script_id"], item["condition"])
        job_id = item["asr_job_id"]
        try:
            job = await asr.job_state(job_id=job_id, authorization=authorization)
        except AsrClientError as exc:
            _raise_if_forbidden(exc)
            # Transient: leave the item transcribing and try again next tick.
            logger.warning("eval run %s: poll failed (%s)", run_id, exc.code)
            still_running += 1
            continue
        if job.status == "complete":
            try:
                tr = await asr.transcript(job_id=job_id, authorization=authorization)
            except AsrClientError as exc:
                _raise_if_forbidden(exc)
                failed.append((*key, exc.code))
                continue
            model_seen = model_seen or tr.model
            _note_engine(engine_seen, tr, references[key])
            scored.append(_score(references[key], tr))
        elif job.status in ("failed", "cancelled"):
            failed.append((*key, job.error_kind or f"asr_{job.status}"))
        else:
            still_running += 1

    # ── phase 2: write results, claim the next batch ──────────────────
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        for row in scored:
            await eval_repo.mark_item_scored(
                conn,
                run_id=run_id,
                script_id=row["script_id"],
                condition=row["condition"],
                hypothesis=row["hypothesis"],
                wer=row["wer"],
                cer=row["cer"],
                ref_words=row["ref_words"],
                wer_norm=row["wer_norm"],
                cer_norm=row["cer_norm"],
                ref_words_norm=row["ref_words_norm"],
                ref_chars_norm=row["ref_chars_norm"],
                dose_tokens=row["dose_tokens"],
                dose_exact=row["dose_exact"],
                flags=json.dumps(row["flags"]),
                speech_ms=row["speech_ms"],
            )
        for script_id, condition, error in failed:
            await eval_repo.mark_item_failed(
                conn,
                run_id=run_id,
                script_id=script_id,
                condition=condition,
                error=error,
            )
        if model_seen:
            await eval_repo.note_run_model(conn, run_id=run_id, model=model_seen)
        if engine_seen:
            await eval_repo.merge_run_engine(
                conn, run_id=run_id, engine=json.dumps(engine_seen)
            )
        capacity = max(0, settings.eval_max_in_flight - still_running)
        claimed = (
            await eval_repo.claim_pending(conn, run_id=run_id, limit=capacity)
            if capacity
            else []
        )
        audio = {}
        for item in claimed:
            key = (item["script_id"], item["condition"])
            audio[key] = await eval_repo.snapshot_audio(
                conn,
                snapshot_id=snapshot_id,
                script_id=item["script_id"],
                condition=item["condition"],
            )

    # ── phase 3: submit the claimed batch ─────────────────────────────
    submitted: list[tuple[str, str, UUID]] = []
    released: list[tuple[str, str, str]] = []
    submit_failed: list[tuple[str, str, str]] = []
    prompts: dict[tuple[str, str], UUID] = {}
    for item in claimed:
        item_key = (item["script_id"], item["condition"])
        row = audio.get(item_key)
        if row is None or row["audio_wav"] is None:
            # The take was deleted after publication. Not retryable, and not
            # a reason to stop the run: the other utterances still measure
            # something.
            submit_failed.append((*item_key, "audio_missing"))
            continue
        language = row["language"]
        specialty = row["specialty"]
        try:
            key = (language, specialty)
            if key not in prompts:
                prompts[key] = await asr.default_prompt_id(
                    language=language, specialty=specialty, authorization=authorization
                )
            job_id = await asr.submit(
                wav=bytes(row["audio_wav"]),
                prompt_id=prompts[key],
                language=language,
                authorization=authorization,
            )
        except AsrClientError as exc:
            _raise_if_forbidden(exc)
            if exc.code in ("asr_busy", "asr_unreachable"):
                released.append((*item_key, exc.code))
            else:
                submit_failed.append((*item_key, exc.code))
            continue
        submitted.append((*item_key, job_id))

    # ── phase 4: record submissions, finish if nothing is left ────────
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        for script_id, condition, job_id in submitted:
            await eval_repo.mark_item_transcribing(
                conn,
                run_id=run_id,
                script_id=script_id,
                condition=condition,
                asr_job_id=job_id,
            )
        for script_id, condition, error in released:
            await eval_repo.release_item(
                conn,
                run_id=run_id,
                script_id=script_id,
                condition=condition,
                error=error,
            )
        for script_id, condition, error in submit_failed:
            await eval_repo.mark_item_failed(
                conn,
                run_id=run_id,
                script_id=script_id,
                condition=condition,
                error=error,
            )

        counts = await eval_repo.count_by_status(conn, run_id)
        open_items = counts.get("pending", 0) + counts.get("transcribing", 0)
        finished_now = False
        if open_items == 0:
            rows = await eval_repo.scored_items(conn, run_id)
            current = await eval_repo.get_run(conn, run_id)
            assert current is not None
            items_for_stats = [
                {
                    "script_id": r["script_id"],
                    "wer": r["wer"],
                    "cer": r["cer"],
                    "ref_words": r["ref_words"],
                    "ref_chars": r["ref_chars"],
                    "wer_norm": r["wer_norm"],
                    "cer_norm": r["cer_norm"],
                    "ref_words_norm": r["ref_words_norm"],
                    "ref_chars_norm": r["ref_chars_norm"],
                    "dose_tokens": r["dose_tokens"],
                    "dose_exact": r["dose_exact"],
                    "flags": list(_jsonb(r["flags"], [])),
                    "hypothesis": r["hypothesis"],
                    "subset": r["subset"],
                    "language": r["language"],
                    "condition": r["condition"],
                    "paired": r["paired"],
                }
                for r in rows
            ]
            counted, quarantined = eval_flags.partition(items_for_stats)
            summary = eval_stats.summarize(
                counted,
                flagged=quarantined,
                seed=int(current["bootstrap_seed"]),
            )
            summary["skipped"] = counts.get("skipped", 0)
            summary["failed"] = counts.get("failed", 0)
            # Everything needed to reproduce this number, inside the number
            # itself — a summary exported to a file must not depend on the
            # row it came from still existing (§1.3.5).
            summary["conditions"] = {
                "dataset": current["dataset"],
                "normalizer_version": current["normalizer_version"],
                "normalizer_rules_sha256": eval_normalize.RULES_SHA256,
                "corpus_sha256": current["corpus_sha256"],
                "model": model_seen or current["model"],
                "engine": _jsonb(current["engine"], {}),
            }
            await eval_repo.finish_run(
                conn,
                run_id=run_id,
                # A run where nothing could be scored is a failed run, not a
                # run with a WER of nothing. Saying "complete" over an empty
                # summary is how a broken pipeline reads as a good result.
                status="complete" if rows else "failed",
                model=model_seen or current["model"],
                summary=json.dumps(summary, ensure_ascii=False),
            )
            finished_now = True

        run_row = await eval_repo.get_run(conn, run_id)
        item_rows = await eval_repo.list_run_items(conn, run_id)
        counts = await eval_repo.count_by_status(conn, run_id)

    assert run_row is not None
    if finished_now:
        summary_obj = run_row["summary"]
        if isinstance(summary_obj, str):
            summary_obj = json.loads(summary_obj)
        overall = (summary_obj or {}).get("overall", {})
        await state.audit_writer.write_event(
            tenant_id=claims.tid,
            kind=audit_kinds.CORPUS_EVAL_RUN_COMPLETED,
            actor_sub=claims.sub,
            actor_role=(claims.roles[0] if claims.roles else None),
            target_kind="corpus_eval_run",
            target_id=run_id,
            payload={
                "status": run_row["status"],
                "model": run_row["model"],
                "utterances_scored": overall.get("utterances", 0),
                "wer": overall.get("wer"),
                "cer": overall.get("cer"),
            },
        )
    return RunDetailDTO(
        run=_run_dto(run_row, counts), items=[_run_item_dto(r) for r in item_rows]
    )


@router.get("/runs", response_model=RunListDTO)
async def list_runs(
    claims: Annotated[Claims, Depends(requires("corpus.review", "phrase"))],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    dataset: Annotated[Literal["dev", "test"] | None, Query()] = None,
) -> RunListDTO:
    """The run table §7 asks for. Filterable by set, because a table that
    sorts dev and test runs together by WER invites reading the best row as
    the result."""
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        rows = await eval_repo.list_runs(conn, limit, dataset)
        counts = {r["id"]: await eval_repo.count_by_status(conn, r["id"]) for r in rows}
    return RunListDTO(items=[_run_dto(r, counts[r["id"]]) for r in rows])


@router.get("/runs/{run_id}", response_model=RunDetailDTO)
async def run_detail(
    run_id: UUID,
    claims: Annotated[Claims, Depends(requires("corpus.review", "phrase"))],
) -> RunDetailDTO:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        run = await eval_repo.get_run(conn, run_id)
        if run is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, detail={"error": "unknown_run"}
            )
        counts = await eval_repo.count_by_status(conn, run_id)
        items = await eval_repo.list_run_items(conn, run_id)
        # "еталон змінено" (Epic B). A score is a claim about a comparison
        # between this audio and THAT reference; if the reference has since
        # been rewritten, the score is still a fact but it no longer says
        # what a reader would assume. Derived at read time so no historical
        # row is ever rewritten to record that history was rewritten.
        revised = await eval_repo.script_ids_revised_since(
            conn, since=run["started_at"]
        )
    return RunDetailDTO(
        run=_run_dto(run, counts),
        items=[_run_item_dto(r, gold_revised=r["script_id"] in revised) for r in items],
    )


# ══ gold-transcript style sweep (Epic B) ═══════════════════════════════


class GoldLintLineDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    script_id: str
    language: str
    dataset: str
    source: str
    say: str
    #: Before → after, which is the whole point of a preview.
    transcript: str
    suggestion: str | None
    findings: list[GoldLintFindingDTO]
    #: Would applying this change what is MEASURED, or only how it is
    #: written? False is the flag worth reading twice.
    canonical_equal: bool | None
    #: Vendored lines are repository data — the sweep reports them and
    #: refuses to apply, because changing them is a commit, not a request.
    applicable: bool
    #: True when applying needs `confirm_test_set` (the frozen holdout).
    needs_test_confirmation: bool


class GoldLintReportDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lines_total: int
    flagged: int
    fixable: int
    flagged_in_test_set: int
    normalizer_version: str
    items: list[GoldLintLineDTO]


class GoldLintApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    #: None means "every fixable line". Naming ids is the reviewed path.
    script_ids: list[str] | None = Field(default=None, max_length=500)
    #: Required before any line in the frozen test set may be rewritten.
    #: Without it those lines are reported as skipped, not silently changed:
    #: editing the holdout changes the measurement itself.
    confirm_test_set: bool = False


class GoldLintSkipDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    script_id: str
    #: builtin_line_immutable | test_requires_confirmation |
    #: no_safe_suggestion | already_clean | unknown_script_id
    code: str


class GoldLintApplyResultDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    applied: list[str]
    skipped: list[GoldLintSkipDTO]
    #: How many of the applied revisions changed what is measured.
    measurement_changed: int


class GoldRevisionDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    script_id: str
    dataset: str
    old_transcript: str
    new_transcript: str
    reason: str
    normalizer_version: str
    canonical_equal: bool
    revised_by: UUID | None
    created_at: datetime


class GoldRevisionListDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[GoldRevisionDTO]


def _lint_line(line: eval_lines.Line) -> GoldLintLineDTO | None:
    """One line's verdict, or None when it already complies."""
    result = eval_goldlint.lint_gold(
        say=line.say, transcript=line.transcript, language=line.language
    )
    if result.clean:
        return None
    suggestion = result.suggestion
    canonical_equal = None
    if suggestion is not None:
        canonical_equal = eval_normalize.normalize(
            suggestion, line.language
        ) == eval_normalize.normalize(line.transcript, line.language)
    return GoldLintLineDTO(
        script_id=line.script_id,
        language=line.language,
        dataset=line.dataset,
        source=line.source,
        say=line.say,
        transcript=line.transcript,
        suggestion=suggestion,
        findings=[
            GoldLintFindingDTO(code=f.code, detail=f.detail) for f in result.findings
        ],
        canonical_equal=canonical_equal,
        applicable=line.editable and suggestion is not None,
        needs_test_confirmation=line.dataset == eval_lines.TEST,
    )


class GoldLintCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=eval_lines.MAX_TEXT)
    language: str = Field(min_length=2, max_length=5)


@router.post("/gold-lint/check", response_model=GoldLintDTO)
async def gold_lint_check(
    body: GoldLintCheckRequest,
    claims: Annotated[Claims, Depends(requires("corpus.contribute", "phrase"))],
) -> GoldLintDTO:
    """Lint one gold transcript that has not been saved yet.

    The line editor calls this while the author is still looking at the
    field, which is the only moment fixing the style is free. Stateless and
    read-only — it writes nothing and reads nothing, it just runs the same
    rules the sweep runs.

    THE CONSOLE DOES NOT LINT LOCALLY. A second copy of the rules in the
    browser bundle would be a second style guide, and the one that drifts is
    always the one the author actually sees.
    """
    return _lint_dto(eval_goldlint.lint(body.text, body.language))


@router.get("/gold-lint", response_model=GoldLintReportDTO)
async def gold_lint_preview(
    claims: Annotated[Claims, Depends(requires("corpus.contribute", "phrase"))],
) -> GoldLintReportDTO:
    """Every gold transcript that breaks the Epic B style guide, with the
    rewrite that would fix it — the "було → стане" preview.

    Read-only by construction. The sweep exists because a style guide that
    only applies to new lines leaves the corpus permanently half-converted,
    and because the operator has to see the damage before authorising it:
    ten of these revisions change nothing but the writing, and some change
    what the subset measures. ``canonical_equal`` tells them apart, and it
    is computed, not asserted.
    """
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        lines = await eval_lines.all_lines(conn)
    items = [dto for dto in map(_lint_line, lines) if dto is not None]
    return GoldLintReportDTO(
        lines_total=len(lines),
        flagged=len(items),
        fixable=sum(1 for i in items if i.applicable),
        flagged_in_test_set=sum(1 for i in items if i.needs_test_confirmation),
        normalizer_version=eval_normalize.VERSION,
        items=items,
    )


@router.post("/gold-lint/apply", response_model=GoldLintApplyResultDTO)
async def gold_lint_apply(
    body: GoldLintApplyRequest,
    claims: Annotated[Claims, Depends(requires("corpus.contribute", "phrase"))],
) -> GoldLintApplyResultDTO:
    """Accept the proposed rewrites, journalling every one (migration 0093).

    Nothing here computes a new suggestion from the request body — the
    server re-derives it from the stored text, so a client cannot smuggle
    arbitrary gold text through a route whose name says "apply the linter".

    Skips are reported per line rather than failing the batch, because the
    interesting ones are informative: `builtin_line_immutable` means the
    repo's own script needs a commit, and `test_requires_confirmation`
    means somebody has to decide, on purpose, to move the holdout.
    """
    state = get_state()
    await _check_rate_limit(state, claims)
    requested = set(body.script_ids) if body.script_ids is not None else None

    applied: list[str] = []
    skipped: list[GoldLintSkipDTO] = []
    measurement_changed = 0

    async with tenant_connection(state.app_pool, claims.tid) as conn:
        lines = await eval_lines.all_lines(conn)
        by_id = {line.script_id: line for line in lines}
        if requested is not None:
            for missing in sorted(requested - by_id.keys()):
                skipped.append(
                    GoldLintSkipDTO(script_id=missing, code="unknown_script_id")
                )

        for line in lines:
            if requested is not None and line.script_id not in requested:
                continue
            verdict = _lint_line(line)
            if verdict is None:
                if requested is not None:
                    skipped.append(
                        GoldLintSkipDTO(
                            script_id=line.script_id, code="already_clean"
                        )
                    )
                continue
            if not line.editable:
                skipped.append(
                    GoldLintSkipDTO(
                        script_id=line.script_id, code="builtin_line_immutable"
                    )
                )
                continue
            if verdict.suggestion is None:
                skipped.append(
                    GoldLintSkipDTO(
                        script_id=line.script_id, code="no_safe_suggestion"
                    )
                )
                continue
            if line.dataset == eval_lines.TEST and not body.confirm_test_set:
                skipped.append(
                    GoldLintSkipDTO(
                        script_id=line.script_id, code="test_requires_confirmation"
                    )
                )
                continue

            await eval_repo.update_script_item(
                conn,
                script_id=line.script_id,
                language=line.language,
                specialty=line.specialty,
                subset=line.subset,
                say=line.say,
                transcript=verdict.suggestion,
                condition=line.condition,
                dataset=None,
            )
            await eval_repo.insert_gold_revision(
                conn,
                tenant_id=claims.tid,
                script_id=line.script_id,
                dataset=line.dataset,
                old_transcript=line.transcript,
                new_transcript=verdict.suggestion,
                reason="style_migration",
                normalizer_version=eval_normalize.VERSION,
                canonical_equal=bool(verdict.canonical_equal),
                revised_by=claims.sub,
            )
            applied.append(line.script_id)
            if not verdict.canonical_equal:
                measurement_changed += 1

    if applied:
        await state.audit_writer.write_event(
            tenant_id=claims.tid,
            kind=audit_kinds.CORPUS_EVAL_GOLD_REVISED,
            actor_sub=claims.sub,
            actor_role=(claims.roles[0] if claims.roles else None),
            target_kind="corpus_eval_line",
            target_id=None,
            # Never the text: an audit payload is not a place to duplicate
            # corpus content, and the journal table already holds both sides.
            payload={
                "applied": len(applied),
                "script_ids": applied[:50],
                "confirm_test_set": body.confirm_test_set,
                "measurement_changed": measurement_changed,
                "normalizer_version": eval_normalize.VERSION,
            },
            # A revision that only changes the writing is routine. One that
            # changes what the subset measures is the kind of thing somebody
            # should be able to find later without knowing to look for it.
            severity=Severity.WARN if measurement_changed else Severity.INFO,
        )
    return GoldLintApplyResultDTO(
        applied=applied, skipped=skipped, measurement_changed=measurement_changed
    )


@router.get("/gold-revisions", response_model=GoldRevisionListDTO)
async def list_gold_revisions(
    claims: Annotated[Claims, Depends(requires("corpus.review", "phrase"))],
    script_id: Annotated[str | None, Query(max_length=120)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> GoldRevisionListDTO:
    """The reference-change journal — what moved, when, and whether it moved
    the measurement with it."""
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        rows = await eval_repo.list_gold_revisions(
            conn, script_id=script_id, limit=limit
        )
    return GoldRevisionListDTO(
        items=[
            GoldRevisionDTO(
                id=r["id"],
                script_id=r["script_id"],
                dataset=r["dataset"],
                old_transcript=r["old_transcript"],
                new_transcript=r["new_transcript"],
                reason=r["reason"],
                normalizer_version=r["normalizer_version"],
                canonical_equal=r["canonical_equal"],
                revised_by=r["revised_by"],
                created_at=r["created_at"],
            )
            for r in rows
        ]
    )


# ══ CSV import (§6) ════════════════════════════════════════════════════


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str = Field(min_length=1, max_length=255)
    #: The file itself, base64. ~4 MiB of CSV is far more than 86 lines.
    csv_base64: str = Field(min_length=8, max_length=6_000_000)
    #: True previews, False commits. Defaults to previewing — an import that
    #: writes by default is one misclick away from 86 unwanted lines.
    dry_run: bool = True
    #: Required before any row may claim `set=test` (§1.2 holdout).
    allow_test: bool = False


class ImportRowResultDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    line_no: int
    script_id: str
    code: str
    field: str | None = None


class ImportReportDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dry_run: bool
    filename: str
    file_sha256: str
    rows_total: int
    rows_added: int
    rows_skipped: int
    rows_rejected: int
    added: list[str]
    skipped: list[ImportRowResultDTO]
    rejected: list[ImportRowResultDTO]
    warnings: list[dict[str, Any]]
    coverage: list[dict[str, Any]]


class ImportJournalEntryDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    filename: str
    file_sha256: str
    dry_run: bool
    rows_total: int
    rows_added: int
    rows_skipped: int
    rows_rejected: int
    imported_by: UUID
    created_at: datetime


class ImportJournalDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[ImportJournalEntryDTO]


@router.post("/import", response_model=ImportReportDTO)
async def import_csv(
    body: ImportRequest,
    claims: Annotated[Claims, Depends(requires("corpus.contribute", "phrase"))],
) -> ImportReportDTO:
    """Import replicas from a §6 CSV — preview first, commit second.

    ONE TRANSACTION. ``tenant_connection`` wraps the whole block, so a file
    that fails halfway leaves nothing behind: 86 rows or none.

    A dry run walks the identical path — same parse, same existence check,
    same skip list — and stops short of the inserts. There is one
    implementation of "what would happen", so the preview cannot disagree
    with the commit about which rows are refused and why.

    IDEMPOTENT BY ID. A row whose id the tenant already has is skipped, not
    duplicated and not updated: re-importing a corrected file adds the new
    lines and leaves the recorded ones alone, because changing the text under
    a take that was already read aloud is how audio starts claiming to be a
    reading of something else.
    """
    state = get_state()
    await _check_rate_limit(state, claims)

    try:
        raw = base64.b64decode(body.csv_base64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"error": "bad_base64"}
        ) from None
    file_sha = hashlib.sha256(raw).hexdigest()
    try:
        parsed = eval_import.parse(
            eval_import.decode(raw), allow_test=body.allow_test
        )
    except eval_import.CsvFormatError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": exc.code, "detail": exc.detail},
        ) from None

    added: list[str] = []
    skipped: list[ImportRowResultDTO] = []
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        existing = await eval_repo.existing_script_ids(
            conn, [r.script_id for r in parsed.rows]
        )
        to_insert = [r for r in parsed.rows if r.script_id not in existing]
        skipped = [
            ImportRowResultDTO(
                line_no=r.line_no,
                script_id=r.script_id,
                code="already_exists",
                field="id",
            )
            for r in parsed.rows
            if r.script_id in existing
        ]
        if not body.dry_run:
            for row in to_insert:
                rec = await eval_repo.insert_script_item_with_id(
                    conn,
                    tenant_id=claims.tid,
                    script_id=row.script_id,
                    language=row.language,
                    specialty=row.specialty,
                    subset=row.subset,
                    say=row.say,
                    transcript=row.transcript,
                    condition=row.condition,
                    source=eval_lines.AUTHORED,
                    created_by=claims.sub,
                    dataset=row.dataset,
                    paired=row.paired,
                )
                if rec is None:
                    # Lost a race with a concurrent import of the same file.
                    skipped.append(
                        ImportRowResultDTO(
                            line_no=row.line_no,
                            script_id=row.script_id,
                            code="already_exists",
                            field="id",
                        )
                    )
                    continue
                added.append(row.script_id)
        else:
            added = [r.script_id for r in to_insert]

        current = [dict(r) for r in await eval_repo.coverage_by_category(conn)]
        coverage = eval_import.coverage_matrix(
            current, to_insert if body.dry_run else []
        )
        journal = await eval_repo.insert_import(
            conn,
            tenant_id=claims.tid,
            filename=body.filename,
            file_sha256=file_sha,
            dry_run=body.dry_run,
            rows_total=parsed.total,
            rows_added=len(added),
            rows_skipped=len(skipped),
            rows_rejected=len(parsed.rejected),
            report=json.dumps(
                {
                    "added": added,
                    "skipped": [s.model_dump() for s in skipped],
                    "rejected": [
                        {
                            "line_no": r.line_no,
                            "script_id": r.script_id,
                            "code": r.code,
                            "field": r.field,
                        }
                        for r in parsed.rejected
                    ],
                    "warnings": parsed.warnings,
                    "coverage": coverage,
                },
                ensure_ascii=False,
            ),
            imported_by=claims.sub,
        )
        # Epic F: a COMMITTED import is a dataset the platform now measures
        # against, so it registers itself. A dry run is not — nothing was
        # added, and a register entry for a file we previewed and discarded
        # would be a claim about data that does not exist.
        if not body.dry_run and added:
            await eval_repo.upsert_registry_entry(
                conn,
                tenant_id=claims.tid,
                **eval_register.import_entry(
                    filename=body.filename,
                    file_sha256=file_sha,
                    import_id=journal["id"],
                    rows_added=len(added),
                ),
            )

    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.CORPUS_EVAL_IMPORTED,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="corpus_eval_import",
        target_id=journal["id"],
        payload={
            "filename": body.filename,
            "file_sha256": file_sha,
            "dry_run": body.dry_run,
            "rows_total": parsed.total,
            "rows_added": len(added),
            "rows_skipped": len(skipped),
            "rows_rejected": len(parsed.rejected),
            "allow_test": body.allow_test,
        },
    )
    return ImportReportDTO(
        dry_run=body.dry_run,
        filename=body.filename,
        file_sha256=file_sha,
        rows_total=parsed.total,
        rows_added=len(added),
        rows_skipped=len(skipped),
        rows_rejected=len(parsed.rejected),
        added=added,
        skipped=skipped,
        rejected=[
            ImportRowResultDTO(
                line_no=r.line_no, script_id=r.script_id, code=r.code, field=r.field
            )
            for r in parsed.rejected
        ],
        warnings=parsed.warnings,
        coverage=coverage,
    )


@router.get("/imports", response_model=ImportJournalDTO)
async def list_imports(
    claims: Annotated[Claims, Depends(requires("corpus.review", "phrase"))],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ImportJournalDTO:
    """Every import, previews included (§6: "звіт про кожен імпорт")."""
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        rows = await eval_repo.list_imports(conn, limit)
    return ImportJournalDTO(
        items=[
            ImportJournalEntryDTO(
                id=r["id"],
                filename=r["filename"],
                file_sha256=r["file_sha256"],
                dry_run=r["dry_run"],
                rows_total=int(r["rows_total"]),
                rows_added=int(r["rows_added"]),
                rows_skipped=int(r["rows_skipped"]),
                rows_rejected=int(r["rows_rejected"]),
                imported_by=r["imported_by"],
                created_at=r["created_at"],
            )
            for r in rows
        ]
    )


# ══ attempt journal (§7) ═══════════════════════════════════════════════


class AttemptDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    script_id: str
    attempt_n: int
    take_id: UUID | None
    speaker: UUID
    device: str | None
    condition: str
    duration_ms: int
    status: str
    reason: str | None
    #: A saved attempt that a later saved attempt replaced. Derived on read,
    #: never stored — the journal is insert-only.
    superseded: bool
    created_at: datetime


class AttemptSummaryDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    script_id: str
    attempts: int
    saved: int
    discarded: int
    rejected: int
    speakers: int
    last_attempt_at: datetime


class AttemptJournalDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[AttemptDTO]
    per_line: list[AttemptSummaryDTO]


@router.get("/attempts", response_model=AttemptJournalDTO)
async def list_attempts(
    claims: Annotated[Claims, Depends(requires("corpus.review", "phrase"))],
    script_id: Annotated[str | None, Query(max_length=120)] = None,
    limit: Annotated[int, Query(ge=1, le=2000)] = 500,
) -> AttemptJournalDTO:
    """The recording journal: every attempt, kept or not (§7).

    ``per_line`` is the question it exists to answer — how many takes a line
    actually cost, and how many different voices have read it. A line at six
    attempts and one speaker is a line whose text is hard to read aloud; a
    line at two attempts and three speakers is the variance decomposition
    §1.2 asks for.
    """
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        rows = await eval_repo.list_take_attempts(
            conn, script_id=script_id, limit=limit
        )
        per_line = await eval_repo.attempt_counts(conn)
    return AttemptJournalDTO(
        items=[
            AttemptDTO(
                id=r["id"],
                script_id=r["script_id"],
                attempt_n=int(r["attempt_n"]),
                take_id=r["take_id"],
                speaker=r["speaker"],
                device=r["device"],
                condition=r["condition"],
                duration_ms=int(r["duration_ms"]),
                status=r["status"],
                reason=r["reason"],
                superseded=bool(r["superseded"]),
                created_at=r["created_at"],
            )
            for r in rows
        ],
        per_line=[
            AttemptSummaryDTO(
                script_id=r["script_id"],
                attempts=int(r["attempts"]),
                saved=int(r["saved"]),
                discarded=int(r["discarded"]),
                rejected=int(r["rejected"]),
                speakers=int(r["speakers"]),
                last_attempt_at=r["last_attempt_at"],
            )
            for r in per_line
        ],
    )


# ══ run comparison (§7) ════════════════════════════════════════════════


class CompareDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    baseline_run: RunDTO
    candidate_run: RunDTO
    metric: str
    comparison: dict[str, Any]


@router.get("/compare", response_model=CompareDTO)
async def compare_runs(
    claims: Annotated[Claims, Depends(requires("corpus.review", "phrase"))],
    baseline: Annotated[UUID, Query()],
    candidate: Annotated[UUID, Query()],
    metric: Annotated[Literal["wer", "wer_norm"], Query()] = "wer_norm",
) -> CompareDTO:
    """Δ WER between two runs, with a paired bootstrap CI (§7).

    THREE REFUSALS, and they are the point of the endpoint rather than
    obstacles to it (§7 "правила чесності"):

      · different sets — a dev run against a test run is not a comparison,
        it is two answers to two questions;
      · different snapshots — different audio and different gold text, so any
        difference is unattributable;
      · different normaliser versions — the rules moved under the metric.

    Each is a 409 naming what differs, so the operator can pick a comparable
    pair rather than being told "no".
    """
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        run_a = await eval_repo.get_run(conn, baseline)
        run_b = await eval_repo.get_run(conn, candidate)
        if run_a is None or run_b is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, detail={"error": "unknown_run"}
            )
        if run_a["dataset"] != run_b["dataset"]:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "error": "dataset_mismatch",
                    "baseline": run_a["dataset"],
                    "candidate": run_b["dataset"],
                },
            )
        if run_a["snapshot_id"] != run_b["snapshot_id"]:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "error": "snapshot_mismatch",
                    "baseline": int(run_a["snapshot_version"]),
                    "candidate": int(run_b["snapshot_version"]),
                },
            )
        if (
            metric == "wer_norm"
            and run_a["normalizer_version"] != run_b["normalizer_version"]
        ):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "error": "normalizer_mismatch",
                    "baseline": run_a["normalizer_version"],
                    "candidate": run_b["normalizer_version"],
                    "message": (
                        "normalised scores computed under different rule "
                        "versions are not comparable; compare raw WER instead"
                    ),
                },
            )

        rows_a = await eval_repo.scored_items(conn, baseline)
        rows_b = await eval_repo.scored_items(conn, candidate)
        counts_a = await eval_repo.count_by_status(conn, baseline)
        counts_b = await eval_repo.count_by_status(conn, candidate)

    weight = "ref_words_norm" if metric == "wer_norm" else "ref_words"

    def usable(rows: list[asyncpg.Record]) -> list[dict[str, Any]]:
        # Quarantined utterances are excluded from the comparison for the
        # same reason they are excluded from the averages: a take that
        # hallucinated in one run and not the other would otherwise dominate
        # the delta with a recording problem.
        return [
            {
                "script_id": r["script_id"],
                metric: r[metric],
                weight: r[weight],
                "subset": r["subset"],
            }
            for r in rows
            if not list(_jsonb(r["flags"], []))
        ]

    comparison = eval_stats.paired_delta(
        usable(rows_a),
        usable(rows_b),
        metric=metric,
        weight=weight,
        seed=int(run_a["bootstrap_seed"]),
    )
    return CompareDTO(
        baseline_run=_run_dto(run_a, counts_a),
        candidate_run=_run_dto(run_b, counts_b),
        metric=metric,
        comparison=comparison,
    )


def _raise_if_forbidden(exc: AsrClientError) -> None:
    """A permission failure is about the CALLER, not the utterance.

    ``docs/auth/permissions.csv`` withholds ``asr.*`` from tenant_admin — a
    deliberate PHI boundary this feature does not get to erode. So a
    tenant_admin can record, author and publish, and gets one clear 403 the
    moment they try to score, instead of thirty utterances each failing
    'asr_forbidden' as though the audio were at fault.
    """
    if exc.code == "asr_forbidden":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail={
                "error": "asr_permission_required",
                "message": (
                    "scoring transcribes through asr-service, which requires "
                    "asr.read/asr.write — held by clinician and nurse, not by "
                    "tenant_admin"
                ),
            },
        )
