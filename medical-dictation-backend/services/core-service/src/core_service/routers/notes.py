"""``/notes`` — clinical notes (SOAP / APSO / DAP / free) bound to a patient,
plus the static note-structure catalogue."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from auth import Claims
from db import tenant_connection

from .. import audit_helper, audit_kinds
from ..deps import get_state, requires
from ..domain import notes_repository, patients_repository

router = APIRouter(tags=["notes"])

Structure = Literal["soap", "apso", "dap", "free"]

# Section scaffolding per note structure (was hard-coded in the SPA).
_STRUCTURES: list[dict[str, Any]] = [
    {
        "code": "soap",
        "name": "SOAP",
        "sections": [
            {"key": "subjective", "label": "Subjective"},
            {"key": "objective", "label": "Objective"},
            {"key": "assessment", "label": "Assessment"},
            {"key": "plan", "label": "Plan"},
        ],
    },
    {
        "code": "apso",
        "name": "APSO",
        "sections": [
            {"key": "assessment", "label": "Assessment"},
            {"key": "plan", "label": "Plan"},
            {"key": "subjective", "label": "Subjective"},
            {"key": "objective", "label": "Objective"},
        ],
    },
    {
        "code": "dap",
        "name": "DAP",
        "sections": [
            {"key": "data", "label": "Data"},
            {"key": "assessment", "label": "Assessment"},
            {"key": "plan", "label": "Plan"},
        ],
    },
    {
        "code": "free",
        "name": "Free text",
        "sections": [{"key": "note", "label": "Note"}],
    },
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoteCreate(_Strict):
    patient_id: UUID
    encounter_id: UUID | None = None
    structure: Structure = "soap"
    title: str = ""
    sections: list[dict[str, Any]] = Field(default_factory=list)
    source_session_id: UUID | None = None


class NotePatch(_Strict):
    title: str | None = None
    structure: Structure | None = None
    sections: list[dict[str, Any]] | None = None


class NoteOut(_Strict):
    id: UUID
    patient_id: UUID
    encounter_id: UUID | None
    structure: str
    title: str
    sections: list[dict[str, Any]]
    status: str
    author_id: UUID
    source_session_id: UUID | None
    created_at: datetime
    updated_at: datetime
    signed_at: datetime | None


class NoteList(_Strict):
    items: list[NoteOut]


def _to_out(row: asyncpg.Record) -> NoteOut:
    sections = row["sections"]
    if isinstance(sections, str):
        import json

        sections = json.loads(sections)
    return NoteOut(
        id=row["id"],
        patient_id=row["patient_id"],
        encounter_id=row["encounter_id"],
        structure=row["structure"],
        title=row["title"],
        sections=sections,
        status=row["status"],
        author_id=row["author_id"],
        source_session_id=row["source_session_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        signed_at=row["signed_at"],
    )


@router.get("/note-structures", summary="Available note structures + sections.")
async def list_note_structures(
    claims: Annotated[Claims, Depends(requires("note.read", "note"))],
) -> list[dict[str, Any]]:
    return _STRUCTURES


@router.get("/notes", response_model=NoteList, summary="List notes (optionally by patient).")
async def list_notes(
    claims: Annotated[Claims, Depends(requires("note.read", "note"))],
    patient_id: Annotated[UUID | None, Query()] = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> NoteList:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        rows = await notes_repository.list_notes(
            conn, patient_id=patient_id, status=status_filter, limit=limit
        )
    return NoteList(items=[_to_out(r) for r in rows])


@router.get("/notes/{note_id}", response_model=NoteOut, summary="Fetch one note.")
async def get_note(
    note_id: UUID,
    claims: Annotated[Claims, Depends(requires("note.read", "note"))],
) -> NoteOut:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        row = await notes_repository.get_note(conn, note_id=note_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return _to_out(row)


@router.post(
    "/notes",
    response_model=NoteOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a clinical note; bumps the patient's last-visit.",
)
async def create_note(
    body: NoteCreate,
    claims: Annotated[Claims, Depends(requires("note.write", "note"))],
) -> NoteOut:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        if await patients_repository.get_patient(conn, patient_id=body.patient_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="patient not found"
            )
        row = await notes_repository.create_note(
            conn,
            tenant_id=claims.tid,
            patient_id=body.patient_id,
            encounter_id=body.encounter_id,
            author_id=claims.sub,
            structure=body.structure,
            title=body.title.strip(),
            sections=body.sections,
            source_session_id=body.source_session_id,
        )
        await patients_repository.bump_last_visit(
            conn, patient_id=body.patient_id, when=row["created_at"]
        )
    await audit_helper.emit(
        state,
        claims,
        audit_kinds.NOTE_CREATED,
        target_kind="note",
        target_id=row["id"],
        payload={"patient_id": str(body.patient_id), "structure": body.structure},
    )
    return _to_out(row)


@router.patch("/notes/{note_id}", response_model=NoteOut, summary="Edit a draft note.")
async def patch_note(
    note_id: UUID,
    body: NotePatch,
    claims: Annotated[Claims, Depends(requires("note.write", "note"))],
) -> NoteOut:
    fields: dict[str, Any] = {}
    if body.title is not None:
        fields["title"] = body.title.strip()
    if body.structure is not None:
        fields["structure"] = body.structure
    if body.sections is not None:
        fields["sections"] = body.sections
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        existing = await notes_repository.get_note(conn, note_id=note_id)
        if existing is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        if existing["status"] == "signed":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="note is signed and immutable"
            )
        row = await notes_repository.update_note(conn, note_id=note_id, fields=fields)
    await audit_helper.emit(
        state, claims, audit_kinds.NOTE_UPDATED, target_kind="note", target_id=note_id
    )
    return _to_out(row)


@router.post("/notes/{note_id}/sign", response_model=NoteOut, summary="Sign a note.")
async def sign_note(
    note_id: UUID,
    claims: Annotated[Claims, Depends(requires("note.write", "note"))],
) -> NoteOut:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        row = await notes_repository.sign_note(
            conn, note_id=note_id, when=datetime.now(UTC)
        )
        if row is None:
            existing = await notes_repository.get_note(conn, note_id=note_id)
            if existing is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="note already signed"
            )
    await audit_helper.emit(
        state, claims, audit_kinds.NOTE_SIGNED, target_kind="note", target_id=note_id
    )
    return _to_out(row)
