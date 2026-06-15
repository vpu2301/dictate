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
from auth import Action, Claims, TargetKind
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


@router.post("/sessions", response_model=InitiateSessionResponse)
async def initiate(
    body: InitiateSessionRequest,
    claims: Annotated[Claims, Depends(requires(Action.WRITE, TargetKind.REPORT))],
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
