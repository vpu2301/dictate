"""``/patients/{id}/documents`` — files attached to a patient's record.

A referral letter, a lab PDF, a scan the patient brought on paper. Three
things make this different from an ordinary CRUD surface:

* **The bytes are PHI.** They go through ``libs/storage.EncryptedObjectStore``
  into MinIO — envelope-encrypted, AAD bound to ``tenant_id ‖ document_id`` —
  and this service holds only metadata plus the object URI. Nothing here ever
  hands out a pre-signed URL: a pre-signed URL serves *ciphertext* (ADR-0011),
  which a browser cannot read, so the download is an authenticated proxy that
  decrypts in-process.

* **Reading one is a PHI access.** The download sits behind the same
  break-glass guard as the patient record itself and emits its own audit
  event; a file read that looks like a list call in the trail defeats the
  control.

* **Deleting one is a crypto-shred.** Object first, then the row: a row
  pointing at a missing object is recoverable bookkeeping, an object nobody
  points at is PHI nobody can find to erase. The same ordering the erasure
  engine uses (``erasure/erasers.py``).
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import quote
from uuid import UUID, uuid4

import asyncpg
from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field

from audit import Severity
from auth import Claims
from db import tenant_connection

from .. import audit_helper, audit_kinds
from ..config import settings
from ..deps import get_state, requires
from ..domain import patient_documents_repository as documents_repository
from ..domain import patients_repository
from ._phi_access_guard import PatientAccess, patient_record_access

router = APIRouter(prefix="/patients", tags=["patient-documents"])

CATEGORIES = ("referral", "lab", "imaging", "discharge", "consent", "other")
Category = Literal["referral", "lab", "imaging", "discharge", "consent", "other"]

# What a clinic actually attaches. An allowlist rather than a denylist: this
# is a record attachment, not a file host, and "anything the browser will
# render" is how a stored-XSS lands in a chart.
ALLOWED_CONTENT_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/heic",
    "image/tiff",
    "text/plain",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/dicom",
}

# Served back with a fixed, non-renderable disposition for everything except
# the formats a viewer genuinely needs inline.
_INLINE_TYPES = {"application/pdf", "image/jpeg", "image/png"}

_UNSAFE_FILENAME = re.compile(r"[\\/\x00-\x1f]")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DocumentOut(_Strict):
    id: UUID
    patient_id: UUID
    filename: str
    category: str
    note: str
    content_type: str
    byte_size: int
    sha256: str
    uploaded_by: UUID
    created_at: datetime


class DocumentList(_Strict):
    items: list[DocumentOut]
    # Total for this patient — the tab badge, without paging the list.
    total: int = Field(default=0, ge=0)


def _to_out(row: asyncpg.Record) -> DocumentOut:
    return DocumentOut(
        id=row["id"],
        patient_id=row["patient_id"],
        filename=row["filename"],
        category=row["category"],
        note=row["note"],
        content_type=row["content_type"],
        byte_size=row["byte_size"],
        sha256=row["sha256"],
        uploaded_by=row["uploaded_by"],
        created_at=row["created_at"],
    )


def _http_error(status_code: int, detail: str, **extras: object) -> HTTPException:
    exc = HTTPException(status_code=status_code, detail=detail)
    exc.problem_extras = extras  # type: ignore[attr-defined]
    return exc


def _safe_filename(raw: str) -> str:
    """Keep the name the clinician recognises, minus anything that could
    escape a path or break a Content-Disposition header."""
    name = _UNSAFE_FILENAME.sub("", (raw or "").strip()) or "document"
    return name[:200]


def _object_key(tenant_id: UUID, document_id: UUID) -> str:
    return f"{tenant_id}/{document_id}.enc"


async def _require_patient(conn: asyncpg.Connection, patient_id: UUID) -> asyncpg.Record:
    row = await patients_repository.get_patient(conn, patient_id=patient_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="patient not found")
    if row["status"] == "erased":
        # An erased record accepts nothing new: attaching a file to a
        # tombstone would re-create the PHI Art. 17 just destroyed.
        raise _http_error(
            status.HTTP_409_CONFLICT,
            "this patient record has been erased",
            code="patient_erased",
        )
    return row


async def _audit(
    claims: Claims,
    kind: str,
    document_id: UUID,
    payload: dict[str, object],
    *,
    severity: Severity = Severity.INFO,
) -> None:
    await audit_helper.emit(
        get_state(),
        claims,
        kind,
        target_kind="patient_document",
        target_id=document_id,
        payload=payload,
        severity=severity,
    )


# ── Upload ──────────────────────────────────────────────────────────


@router.post(
    "/{patient_id}/documents",
    response_model=DocumentOut,
    status_code=status.HTTP_201_CREATED,
    summary="Attach a file to a patient's record.",
)
async def upload_document(
    patient_id: UUID,
    claims: Annotated[Claims, Depends(requires("patient.write", "patient"))],
    # Writing to the record presumes being allowed to read it: an admin needs
    # a live break-glass grant, a clinician rides `patient.read_full`.
    access: Annotated[PatientAccess, Depends(patient_record_access)],
    file: Annotated[UploadFile, File()],
    category: Annotated[Category, Form()] = "other",
    note: Annotated[str, Form(max_length=500)] = "",
) -> DocumentOut:
    state = get_state()
    store = state.document_store
    if store is None:
        # No envelope crypto → no upload. Storing the file in the clear
        # instead would be the one failure mode worse than an outage.
        raise _http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "document storage is not available (envelope crypto is not wired)",
            code="document_storage_unavailable",
        )

    content_type = (file.content_type or "application/octet-stream").split(";")[0].strip()
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise _http_error(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            f"{content_type!r} is not an accepted document type",
            code="content_type_rejected",
            allowed=sorted(ALLOWED_CONTENT_TYPES),
        )

    max_bytes = settings.patient_document_max_bytes
    # Read with a ceiling: Content-Length is the client's claim, the bytes
    # are the fact. One extra byte read is enough to know it overflowed.
    payload = await file.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise _http_error(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"a document may be at most {max_bytes} bytes",
            code="document_too_large",
            max_bytes=max_bytes,
        )
    if not payload:
        raise _http_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "the uploaded file is empty",
            code="document_empty",
        )

    document_id = uuid4()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        await _require_patient(conn, patient_id)

    key = _object_key(claims.tid, document_id)
    header = await store.put(
        key=key,
        plaintext=payload,
        tenant_id=claims.tid,
        aad=document_id.bytes,
    )
    storage_uri = f"minio://{store.bucket}/{key}"

    try:
        async with tenant_connection(state.app_pool, claims.tid) as conn:
            row = await documents_repository.create_document(
                conn,
                document_id=document_id,
                tenant_id=claims.tid,
                patient_id=patient_id,
                filename=_safe_filename(file.filename or ""),
                category=category,
                note=note.strip(),
                content_type=content_type,
                byte_size=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
                storage_uri=storage_uri,
                envelope_metadata=json.dumps(
                    {
                        "algorithm": getattr(header, "algorithm", None),
                        "key_id": str(getattr(header, "key_id", "") or ""),
                    }
                ),
                uploaded_by=claims.sub,
            )
    except Exception:
        # The row is what makes the object findable. If it cannot be written,
        # the object is unreachable PHI — delete it rather than leak it.
        await store.delete(key=key)
        raise

    await _audit(
        claims,
        audit_kinds.PATIENT_DOCUMENT_UPLOADED,
        document_id,
        {
            "patient_id": str(patient_id),
            "category": category,
            "content_type": content_type,
            "byte_size": len(payload),
            "break_glass": access.is_break_glass,
        },
        severity=Severity.SEC if access.is_break_glass else Severity.INFO,
    )
    return _to_out(row)


# ── List ────────────────────────────────────────────────────────────


@router.get(
    "/{patient_id}/documents",
    response_model=DocumentList,
    summary="List the files attached to a patient's record.",
)
async def list_documents(
    patient_id: UUID,
    access: Annotated[PatientAccess, Depends(patient_record_access)],
) -> DocumentList:
    state = get_state()
    async with tenant_connection(state.app_pool, access.claims.tid) as conn:
        rows = await documents_repository.list_documents(conn, patient_id=patient_id)
        total = await documents_repository.count_documents(conn, patient_id=patient_id)
    # No audit event: this is a directory listing (names, sizes, dates), the
    # same standing as seeing the record exists. Reading a file is the
    # access, and that one IS audited below.
    return DocumentList(items=[_to_out(r) for r in rows], total=total)


# ── Download ────────────────────────────────────────────────────────


@router.get(
    "/{patient_id}/documents/{document_id}/content",
    summary="Download one attachment (decrypted, authenticated proxy).",
    response_class=Response,
)
async def download_document(
    patient_id: UUID,
    document_id: UUID,
    access: Annotated[PatientAccess, Depends(patient_record_access)],
) -> Response:
    state = get_state()
    store = state.document_store
    if store is None:
        raise _http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "document storage is not available (envelope crypto is not wired)",
            code="document_storage_unavailable",
        )
    async with tenant_connection(state.app_pool, access.claims.tid) as conn:
        row = await documents_repository.get_document(
            conn, document_id=document_id, patient_id=patient_id
        )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="document not found")

    key = row["storage_uri"].split("://", 1)[1].split("/", 1)[1]
    plaintext = await store.get(
        key=key, tenant_id=access.claims.tid, aad=document_id.bytes
    )

    await _audit(
        access.claims,
        audit_kinds.PATIENT_DOCUMENT_DOWNLOADED,
        document_id,
        {
            "patient_id": str(patient_id),
            "byte_size": len(plaintext),
            "break_glass": access.is_break_glass,
        },
        severity=Severity.SEC if access.is_break_glass else Severity.INFO,
    )

    content_type = row["content_type"]
    disposition = "inline" if content_type in _INLINE_TYPES else "attachment"
    # RFC 6266 filename* — a Ukrainian filename is not latin-1 encodable, so a
    # bare `filename=` header would 500 on it. The ASCII fallback is for
    # clients that predate the star form.
    name = _safe_filename(row["filename"])
    ascii_fallback = re.sub(r'[^\x20-\x7e]', "_", name).replace('"', "'")
    return Response(
        content=plaintext,
        media_type=content_type,
        headers={
            "Content-Disposition": (
                f'{disposition}; filename="{ascii_fallback}"; '
                f"filename*=UTF-8''{quote(name, safe='')}"
            ),
            # Never let a proxy or the browser keep PHI on disk.
            "Cache-Control": "no-store",
        },
    )


# ── Delete ──────────────────────────────────────────────────────────


@router.delete(
    "/{patient_id}/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove an attachment (object crypto-shredded, then the row).",
)
async def delete_document(
    patient_id: UUID,
    document_id: UUID,
    claims: Annotated[Claims, Depends(requires("patient.write", "patient"))],
    access: Annotated[PatientAccess, Depends(patient_record_access)],
) -> Response:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        row = await documents_repository.get_document(
            conn, document_id=document_id, patient_id=patient_id
        )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="document not found")

    if state.document_store is not None:
        key = row["storage_uri"].split("://", 1)[1].split("/", 1)[1]
        # Object first — its per-object DEK dies with it. delete() tolerates
        # an absent key, so a retried delete is idempotent.
        await state.document_store.delete(key=key)

    async with tenant_connection(state.app_pool, claims.tid) as conn:
        await documents_repository.delete_document(
            conn, document_id=document_id, patient_id=patient_id
        )

    await _audit(
        claims,
        audit_kinds.PATIENT_DOCUMENT_DELETED,
        document_id,
        {
            "patient_id": str(patient_id),
            "category": row["category"],
            "break_glass": access.is_break_glass,
        },
        severity=Severity.SEC,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
