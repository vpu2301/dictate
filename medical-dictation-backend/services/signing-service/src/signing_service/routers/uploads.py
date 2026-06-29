"""POST /signing/sessions/{id}/upload — locally-signed PDF ingest (M1·B4).

The local-КЕП flow: the FE rendered the unsigned PDF (report-service A3),
the clinician signed it on their machine with an ІІТ token, and now uploads
the signed PAdES bytes here. We reuse the callback ingestion pipeline —
parse + ``verify_envelope`` against the trust store — but bind to the
*session's stored expected hash* (set at initiate from the rendered PDF), a
stronger binding than the provider-callback path.

The uploaded bytes are the SIGNED pdf, so their sha256 differs from the
stored unsigned hash — binding is done entirely by ``verify_envelope``
(which checks the signature covers the expected document), never by a raw
byte compare.
"""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from medical_kep import ProviderName, verify_envelope
from medical_kep.envelope import Envelope, EnvelopeParseError
from pydantic import BaseModel, ConfigDict

from audit import Severity
from auth import Claims
from db import tenant_connection

from .. import audit_kinds
from .. import repository as repo
from ..config import settings
from ..deps import get_state, requires
from ..security import ipn_hmac, new_verification_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/signing", tags=["signing"])


class LocalUploadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: UUID
    status: str
    signed_envelope_id: UUID
    verification_token: str


def _invalid(detail: str) -> HTTPException:
    return HTTPException(
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={
            "type": "https://errors.medical-dictation/signature-invalid",
            "title": "Uploaded signature could not be verified",
            "status": status.HTTP_422_UNPROCESSABLE_ENTITY,
            "detail": detail,
        },
    )


@router.post("/sessions/{session_id}/upload", response_model=LocalUploadResponse)
async def upload_signed_pdf(
    session_id: UUID,
    claims: Annotated[Claims, Depends(requires("report.write", "report"))],
    file: Annotated[UploadFile, File(description="Locally-signed PAdES PDF.")],
) -> LocalUploadResponse:
    state = get_state()

    # ── 1. Session lookup + state gate (no DB writes yet) ───────────
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        row = await repo.fetch_session_by_id(conn, session_id=session_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="session not found")
    if row["status"] != "awaiting_user":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "type": "https://errors.medical-dictation/session-not-awaiting-upload",
                "title": "Signing session does not accept an upload",
                "status": status.HTTP_409_CONFLICT,
                "detail": f"session {session_id} is in status {row['status']!r}, not 'awaiting_user'",
                "session_status": row["status"],
            },
        )

    expected_hash = row["document_pdf_hash"]
    if expected_hash is None:
        # No bound document — a remote-provider session reached here by mistake.
        raise _invalid("session has no bound document hash for local upload")

    # ── 2. File-shape validation (before any DB write) ──────────────
    content_type = file.content_type or "application/octet-stream"
    if content_type != "application/pdf":
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"expected application/pdf, got {content_type!r}",
        )
    pdf_bytes = await file.read()
    cap = settings.max_upload_mb * 1024 * 1024
    if len(pdf_bytes) > cap:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"upload is {len(pdf_bytes)} bytes; cap is {cap} bytes",
        )
    if not pdf_bytes.startswith(b"%PDF"):
        raise _invalid("uploaded bytes are not a PDF")

    # ── 3. Move to verifying (optimistic; races the reaper) ─────────
    tenant_id = row["tenant_id"]
    async with tenant_connection(state.app_pool, tenant_id) as conn:
        moved = await repo.transition_session(
            conn, session_id=session_id, expected_from="awaiting_user", to="verifying"
        )
    if not moved:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail="session changed state before upload"
        )

    # ── 4. Parse + verify the PAdES envelope, bind to expected hash ─
    async def _fail(reason: str) -> None:
        async with tenant_connection(state.app_pool, tenant_id) as conn:
            await repo.transition_session(
                conn,
                session_id=session_id,
                expected_from="verifying",
                to="failed",
                failure_reason=reason[:200],
            )

    try:
        parsed = Envelope(pdf_bytes).parse()
    except EnvelopeParseError as exc:
        await _fail(f"envelope_parse_failed:{exc}")
        raise _invalid(f"could not parse signed PDF: {exc}") from exc

    result = verify_envelope(
        parsed=parsed,
        expected_document_hash=expected_hash,
        trust_store=state.trust_store,
    )
    if not result.valid:
        await _fail("envelope_verification_failed:" + ",".join(result.errors))
        raise _invalid("signature verification failed: " + "; ".join(result.errors))

    # ── 5. Persist envelope + flip to signed ────────────────────────
    ipn = parsed.signer_ipn
    ipn_hmac_bytes = ipn_hmac(ipn, settings.signer_ipn_hmac_key_hex) if ipn else None
    token = new_verification_token()
    provider = ProviderName(row["provider"])
    async with tenant_connection(state.app_pool, tenant_id) as conn:
        envelope_id = await repo.insert_envelope(
            conn,
            tenant_id=tenant_id,
            signer_user_id=row["initiated_by"],
            resource_type=row["resource_type"],
            resource_id=row["resource_id"],
            resource_version_id=row["resource_version_id"],
            provider=provider,
            provider_session_id=row["provider_session_id"],
            provider_envelope_id=f"local-upload:{session_id}",
            canonical_json={},
            canonical_json_hash=parsed.document_hash_sha256,
            signed_at=parsed.signed_at,
            signed_data=pdf_bytes,
            signature_algorithm=parsed.signature_algorithm,
            verification_token=token,
            pdf_storage_uri=None,
            signer_ipn_hmac=ipn_hmac_bytes,
            signer_full_name=parsed.signer_full_name or "",
            certificate_serial=parsed.signer_cert_serial_hex,
            certificate_issuer_cn=parsed.signer_cert_issuer_cn,
            certificate_chain=parsed.cert_chain_pem,
            tsa_response=None,
            ocsp_responses=[],
            is_qualified=parsed.is_qualified,
            ltv_enabled=parsed.tsa_token_present and parsed.ocsp_responses_present,
        )
        await repo.transition_session(
            conn,
            session_id=session_id,
            expected_from="verifying",
            to="signed",
            signed_envelope_id=envelope_id,
        )

    # Reuse the canonical persist event, plus the local-upload marker.
    for kind in (audit_kinds.SIGNING_ENVELOPE_PERSISTED, audit_kinds.SIGNING_SESSION_LOCAL_UPLOAD):
        await state.audit_writer.write_event(
            tenant_id=tenant_id,
            kind=kind,
            actor_sub=row["initiated_by"],
            actor_role=(claims.roles[0] if claims.roles else None),
            target_kind="signing_session",
            target_id=session_id,
            payload={
                "provider": provider.value,
                "signed_envelope_id": str(envelope_id),
                "is_qualified": parsed.is_qualified,
            },
            severity=Severity.INFO,
        )

    return LocalUploadResponse(
        session_id=session_id,
        status="signed",
        signed_envelope_id=envelope_id,
        verification_token=token,
    )
