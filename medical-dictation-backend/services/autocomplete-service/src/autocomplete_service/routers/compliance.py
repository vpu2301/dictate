"""/compliance — the data register and speaker consents (corpus-v3 Epic F).

  GET    /compliance/data-register              the register as JSON
  GET    /compliance/data-register/export.html  the auditor's document
  GET    /compliance/data-register/export.pdf   …as a file
  GET    /compliance/consents                   who consented, and when
  POST   /compliance/consents                   record a consent
  DELETE /compliance/consents/{speaker_id}      withdraw one

WHY THIS IS BEHIND `corpus.review` AND NOT AN ADMIN-ONLY PERMISSION. The
register describes the eval corpus, and the people who need to read it are
the people who maintain that corpus — the same trust circle that already
sees candidate phrases and takes. A separate permission would mean the
person who records a colleague's voice cannot check whether that colleague
consented, which is the wrong way round.

WHY WITHDRAWAL DELETES NOTHING. A consent record is evidence, and evidence
of a withdrawal is the part that matters most. The row is stamped
`revoked_at`, future snapshots stop including that speaker's takes
(``fetch_for_publish``), and published snapshots are untouched — the basis
that existed when they were frozen is not unmade by a later withdrawal.
Erasing the audio itself is the separate, deliberate act the privacy runbook
describes, and it is not a side effect of clicking "withdraw".
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field

from audit import Severity
from auth import Claims
from db import tenant_connection

from .. import audit_kinds, eval_register
from .. import eval_repository as eval_repo
from ..deps import get_state, requires

router = APIRouter(prefix="/compliance", tags=["compliance"])


class DatasetEntryDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    name: str
    version: str
    sha256: str
    purpose: str
    data_origin: str
    #: Always false, enforced by a CHECK constraint in migration 0097.
    contains_patient_data: bool
    #: Usually TRUE — a voice is personal data whatever the script says.
    contains_personal_data: bool
    speakers: list[UUID]
    legal_basis: str
    retention_period: str
    storage_location: str
    frozen: bool
    source_kind: str
    source_id: UUID | None
    utterances: int | None
    created_at: datetime
    updated_at: datetime


class ConsentDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    speaker_id: UUID
    scope: str
    granted_at: datetime
    granted_by: UUID | None
    revoked_at: datetime | None
    revoked_by: UUID | None
    note: str | None
    #: How many takes this speaker has recorded — what a withdrawal affects.
    takes_recorded: int = 0

    @property
    def active(self) -> bool:
        return self.revoked_at is None


class DataRegisterDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    datasets: list[DatasetEntryDTO]
    consents: list[ConsentDTO]
    generated_at: datetime
    #: Repeated in every representation so it cannot be separated from the
    #: data by a screenshot.
    disclaimer: str


class GrantConsentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    #: Omitted means "myself", which is the common case by a wide margin: a
    #: recordist consenting to their own voice should not have to look up
    #: their own subject id, and the self path is the one the recorder hits
    #: when a first take is refused.
    speaker_id: UUID | None = None
    note: str | None = Field(default=None, max_length=500)


DISCLAIMER = (
    "Реєстр — інженерний інструмент прозорості, а не юридичний висновок. "
    "Класифікацію системи за EU AI Act має підтвердити юрист."
)


def _entry_dto(row: Any) -> DatasetEntryDTO:
    return DatasetEntryDTO(
        id=row["id"],
        name=row["name"],
        version=row["version"],
        sha256=row["sha256"],
        purpose=row["purpose"],
        data_origin=row["data_origin"],
        contains_patient_data=row["contains_patient_data"],
        contains_personal_data=row["contains_personal_data"],
        speakers=list(row["speakers"] or ()),
        legal_basis=row["legal_basis"],
        retention_period=row["retention_period"],
        storage_location=row["storage_location"],
        frozen=row["frozen"],
        source_kind=row["source_kind"],
        source_id=row["source_id"],
        utterances=row["utterances"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _consent_dto(row: Any, *, takes_recorded: int | None = None) -> ConsentDTO:
    """``takes_recorded`` is supplied by the listing query; the grant and
    revoke paths return the freshly-written row, which has no such column —
    they pass it explicitly rather than merging a dict into an asyncpg
    Record, which is not a dict and does not support ``|``."""
    if takes_recorded is None:
        takes_recorded = int(row["takes_recorded"] or 0)
    return ConsentDTO(
        id=row["id"],
        speaker_id=row["speaker_id"],
        scope=row["scope"],
        granted_at=row["granted_at"],
        granted_by=row["granted_by"],
        revoked_at=row["revoked_at"],
        revoked_by=row["revoked_by"],
        note=row["note"],
        takes_recorded=takes_recorded,
    )


async def _load(claims: Claims) -> tuple[list[Any], list[Any]]:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        return (
            await eval_repo.list_registry(conn),
            await eval_repo.list_consents(conn),
        )


@router.get("/data-register", response_model=DataRegisterDTO)
async def data_register(
    claims: Annotated[Claims, Depends(requires("corpus.review", "phrase"))],
) -> DataRegisterDTO:
    """Every dataset this platform measures itself against, and whose voices
    are in each."""
    entries, consents = await _load(claims)
    return DataRegisterDTO(
        datasets=[_entry_dto(r) for r in entries],
        consents=[_consent_dto(r) for r in consents],
        generated_at=datetime.now(UTC),
        disclaimer=DISCLAIMER,
    )


def _document(claims: Claims, entries: list[Any], consents: list[Any]) -> str:
    return eval_register.render_html(
        entries=[dict(r) for r in entries],
        consents=[dict(r) for r in consents],
        generated_at=datetime.now(UTC),
        tenant_id=claims.tid,
    )


@router.get("/data-register/export.html", response_class=Response)
async def export_html(
    claims: Annotated[Claims, Depends(requires("corpus.review", "phrase"))],
) -> Response:
    """The same document the PDF renders, always available.

    The PDF needs native libraries the image may not carry; this never does,
    so an auditor is never blocked on a deployment detail.
    """
    entries, consents = await _load(claims)
    return Response(
        content=_document(claims, entries, consents),
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/data-register/export.pdf", response_class=Response)
async def export_pdf(
    claims: Annotated[Claims, Depends(requires("corpus.review", "phrase"))],
) -> Response:
    """The register as a file to hand over."""
    entries, consents = await _load(claims)
    document = _document(claims, entries, consents)
    try:
        blob = eval_register.render_pdf(document)
    except eval_register.PdfUnavailableError as exc:
        # Better a clear refusal with a working alternative than a file that
        # will not open. The `pdf` extra and its native deps are the fix.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "pdf_renderer_unavailable",
                "detail": str(exc)[:200],
                "alternative": "/compliance/data-register/export.html",
            },
        ) from None

    state = get_state()
    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.CORPUS_DATA_REGISTER_EXPORTED,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="dataset_registry",
        target_id=None,
        payload={"datasets": len(entries), "consents": len(consents), "format": "pdf"},
    )
    return Response(
        content=blob,
        media_type="application/pdf",
        headers={
            "Content-Disposition": 'attachment; filename="klarnote-data-register.pdf"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/consents", response_model=list[ConsentDTO])
async def list_consents(
    claims: Annotated[Claims, Depends(requires("corpus.review", "phrase"))],
) -> list[ConsentDTO]:
    """Consent history including withdrawals — a table showing only the
    current state cannot answer "was this lawful in March"."""
    _, consents = await _load(claims)
    return [_consent_dto(r) for r in consents]


@router.post("/consents", response_model=ConsentDTO, status_code=status.HTTP_201_CREATED)
async def grant_consent(
    body: GrantConsentRequest,
    claims: Annotated[Claims, Depends(requires("corpus.review", "phrase"))],
) -> ConsentDTO:
    """Record that a speaker consented to their voice being in the corpus.

    With no ``speaker_id`` this records the CALLER's own consent — what the
    recorder asks for when a first take is refused, in the words that
    describe what is being agreed to.
    """
    state = get_state()
    speaker_id = body.speaker_id or claims.sub
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        row = await eval_repo.grant_consent(
            conn,
            tenant_id=claims.tid,
            speaker_id=speaker_id,
            granted_by=claims.sub,
            note=body.note,
        )
    if row is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail={"error": "consent_already_active"}
        )
    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.CORPUS_SPEAKER_CONSENT_GRANTED,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="speaker_consent",
        target_id=row["id"],
        payload={
            "speaker_id": str(speaker_id),
            "scope": "corpus_voice",
            "self": body.speaker_id is None,
        },
    )
    return _consent_dto(row, takes_recorded=0)


@router.delete("/consents/{speaker_id}", response_model=ConsentDTO)
async def revoke_consent(
    speaker_id: UUID,
    claims: Annotated[Claims, Depends(requires("corpus.review", "phrase"))],
) -> ConsentDTO:
    """Withdraw a consent. The speaker's takes drop out of FUTURE snapshots;
    published ones are unchanged, and no audio is deleted here."""
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        row = await eval_repo.revoke_consent(
            conn, speaker_id=speaker_id, revoked_by=claims.sub
        )
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail={"error": "no_active_consent"}
        )
    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.CORPUS_SPEAKER_CONSENT_REVOKED,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="speaker_consent",
        target_id=row["id"],
        payload={"speaker_id": str(speaker_id), "scope": "corpus_voice"},
        # It changes which takes may enter future measurements. Somebody
        # should be able to find this without knowing to look for it.
        severity=Severity.WARN,
    )
    return _consent_dto(row, takes_recorded=0)
