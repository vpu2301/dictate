"""POST /signing/sessions — internal route, service-account JWT."""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from medical_kep import (
    DocumentDisplayMetadata,
    ProviderName,
    SignerHint,
)
from medical_kep.selection import select_providers
from pydantic import BaseModel, ConfigDict, Field

from audit import Severity
from auth import Claims
from db import tenant_connection

from .. import audit_kinds
from .. import repository as repo
from ..deps import get_state, requires

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/signing", tags=["signing"])


class InitiateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource_type: str = Field(pattern=r"^(report|amendment|note|anamnesis|consent)$")
    resource_id: UUID
    resource_version_id: UUID
    document_pdf_hash_hex: str = Field(min_length=64, max_length=64)
    display: dict
    callback_completion_url: str | None = None
    user_provider_choice: ProviderName | None = None
    signer_hint: dict | None = None
    purpose_code: str | None = None


class InitiateSessionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: UUID
    provider: ProviderName
    expires_at: str
    redirect_url: str | None = None
    qr_payload: str | None = None
    local_helper_payload: dict | None = None
    available_providers: list[ProviderName]


class SessionStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: UUID
    status: str  # backend enum verbatim; FE maps signed→complete, verifying→signing
    provider: ProviderName
    expires_at: str
    redirect_url: str | None = None
    qr_payload: str | None = None
    signed_envelope_id: UUID | None = None
    failure_reason: str | None = None
    verification_token: str | None = None
    signed_at: str | None = None
    signer_full_name: str | None = None


class CancelSessionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str


# Only a session the user hasn't committed to a provider yet may be cancelled.
_CANCELLABLE_FROM = ("awaiting_user", "initiating")


@router.post("/sessions", response_model=InitiateSessionResponse)
async def initiate(
    body: InitiateSessionRequest,
    claims: Annotated[Claims, Depends(requires("report.write", "report"))],
) -> InitiateSessionResponse:
    state = get_state()

    async with tenant_connection(state.app_pool, claims.tid) as conn:
        provider_health_rows = await repo.fetch_all_provider_health(conn)
    health_map = {ProviderName(p): healthy for p, healthy in provider_health_rows.items()}
    # If we have no health rows yet, assume providers that are wired
    # are healthy (boot-time situation).
    for p in state.providers.providers:
        health_map.setdefault(p, True)

    selection = select_providers(
        health=health_map,
        user_choice=body.user_provider_choice,
        allow_mock=state.providers.providers.get(ProviderName.MOCK) is not None,
    )
    if selection.chosen is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "no_signing_provider_available",
                "unhealthy": [p.value for p in selection.unhealthy],
            },
        )

    provider = state.providers.get(selection.chosen)
    display = DocumentDisplayMetadata(
        title=str(body.display.get("title", ""))[:200],
        report_code=str(body.display.get("code", ""))[:64],
        issuer_name=str(body.display.get("issuer", ""))[:200],
        encounter_date_iso=body.display.get("encounter_date"),
        page_count=int(body.display.get("page_count", 1)),
        sha256_hex=body.document_pdf_hash_hex,
        language=body.display.get("language", "uk"),
    )
    hint = None
    if body.signer_hint:
        hint = SignerHint(
            full_name=body.signer_hint.get("full_name"),
            ipn_hmac_hex=body.signer_hint.get("ipn_hmac_hex"),
            expected_cert_subject_cn=body.signer_hint.get("expected_cert_subject_cn"),
        )

    init = await provider.initiate(
        document_pdf_hash=bytes.fromhex(body.document_pdf_hash_hex),
        display=display,
        signer_hint=hint,
        callback_url=body.callback_completion_url or "/signing/callbacks/" + selection.chosen.value,
    )

    async with tenant_connection(state.app_pool, claims.tid) as conn:
        session_id = await repo.insert_session(
            conn,
            tenant_id=claims.tid,
            initiated_by=claims.sub,
            resource_type=body.resource_type,
            resource_id=body.resource_id,
            resource_version_id=body.resource_version_id,
            provider=selection.chosen,
            provider_session_id=init.provider_session_id,
            expires_at=init.expires_at,
            redirect_url=init.redirect_url,
            qr_payload=init.qr_payload,
            callback_completion_url=body.callback_completion_url,
            purpose_code=body.purpose_code,
            document_pdf_hash=bytes.fromhex(body.document_pdf_hash_hex),
        )

    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.SIGNING_SESSION_INITIATED,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="signing_session",
        target_id=session_id,
        payload={
            "provider": selection.chosen.value,
            "resource_type": body.resource_type,
            "resource_id": str(body.resource_id),
            "resource_version_id": str(body.resource_version_id),
        },
        severity=Severity.INFO,
    )

    return InitiateSessionResponse(
        session_id=session_id,
        provider=selection.chosen,
        expires_at=init.expires_at.isoformat(),
        redirect_url=init.redirect_url,
        qr_payload=init.qr_payload,
        local_helper_payload=init.local_helper_payload,
        available_providers=selection.available,
    )


@router.get("/sessions/{session_id}", response_model=SessionStatusResponse)
async def get_session(
    session_id: UUID,
    claims: Annotated[Claims, Depends(requires("report.read", "report"))],
) -> SessionStatusResponse:
    """Poll a signing session's status (M1·B1).

    Status advances via the provider webhook callback — this just reads the
    DB row (tenant-scoped, RLS enforces ownership → 404 cross-tenant).
    """
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        row = await repo.fetch_session_by_id(conn, session_id=session_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="session not found")

    signed_at = row["signed_at"]
    return SessionStatusResponse(
        session_id=row["id"],
        status=row["status"],
        provider=ProviderName(row["provider"]),
        expires_at=row["expires_at"].isoformat(),
        redirect_url=row["redirect_url"],
        qr_payload=row["qr_payload"],
        signed_envelope_id=row["signed_envelope_id"],
        failure_reason=row["failure_reason"],
        verification_token=row["verification_token"],
        signed_at=signed_at.isoformat() if signed_at else None,
        signer_full_name=row["signer_full_name"],
    )


@router.delete("/sessions/{session_id}", response_model=CancelSessionResponse)
async def cancel_session(
    session_id: UUID,
    claims: Annotated[Claims, Depends(requires("report.write", "report"))],
) -> CancelSessionResponse:
    """Cancel an in-flight signing session (M1·B2).

    Cancellable only from {initiating, awaiting_user}; never once the
    document is verifying/signed. The optimistic ``transition_session``
    races cleanly against ``jobs/reaper.py`` — if the session has already
    moved on, the transition no-ops and we return 409.
    """
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        row = await repo.fetch_session_by_id(conn, session_id=session_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="session not found")
        if row["status"] not in _CANCELLABLE_FROM:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "type": "https://errors.medical-dictation/session-not-cancellable",
                    "title": "Signing session cannot be cancelled",
                    "status": status.HTTP_409_CONFLICT,
                    "detail": (
                        f"session {session_id} is in status {row['status']!r}; "
                        "only initiating/awaiting_user sessions can be cancelled"
                    ),
                    "session_status": row["status"],
                },
            )
        cancelled = await repo.transition_session(
            conn,
            session_id=session_id,
            expected_from=row["status"],
            to="cancelled",
            failure_reason="user_cancelled",
        )
        if not cancelled:
            # Raced with the reaper / a callback between fetch and update.
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "type": "https://errors.medical-dictation/session-not-cancellable",
                    "title": "Signing session cannot be cancelled",
                    "status": status.HTTP_409_CONFLICT,
                    "detail": f"session {session_id} changed state before it could be cancelled",
                },
            )

    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.SIGNING_SESSION_CANCELLED,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="signing_session",
        target_id=session_id,
        payload={"from_status": row["status"]},
        severity=Severity.INFO,
    )
    return CancelSessionResponse(status="cancelled")
