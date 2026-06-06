"""POST /signing/callbacks/{provider}."""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from audit import Severity
from db import tenant_connection
from medical_kep import (
    InvalidCallbackError,
    ProviderName,
    verify_envelope,
)
from medical_kep.envelope import Envelope, EnvelopeFormat

from .. import audit_kinds
from .. import repository as repo
from ..deps import get_state
from ..security import ipn_hmac, new_verification_token
from ..config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/signing/callbacks", tags=["signing"])


@router.post("/{provider}")
async def receive_callback(
    provider: str,
    request: Request,
) -> dict[str, str]:
    state = get_state()
    try:
        provider_name = ProviderName(provider)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown provider")

    try:
        provider_impl = state.providers.get(provider_name)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="provider not configured")

    body = await request.body()
    headers = {k: v for k, v in request.headers.items()}

    # 1) Look up session (callback writer pool).
    session_id_str = request.query_params.get("session_id") or request.headers.get(
        "X-Provider-Session-Id"
    )
    if not session_id_str:
        # Best effort: extract from body if provider posts it there.
        # Sprint-09 ships with explicit query/header passing.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="missing provider session id"
        )

    async with state.callback_writer_pool.acquire() as conn:
        session_row = await repo.fetch_session_by_provider_id(
            conn, provider=provider_name, provider_session_id=session_id_str
        )
        if session_row is None:
            return _no_info_response(404)
        if session_row["status"] not in ("awaiting_user", "verifying", "initiating"):
            return _no_info_response(410)
        await repo.transition_session(
            conn,
            session_id=session_row["id"],
            expected_from=session_row["status"],
            to="verifying",
        )

    # 2) Provider-specific callback handle (signature verification + envelope fetch).
    try:
        envelope = await provider_impl.handle_callback(
            provider_session_id=session_id_str,
            callback_body=body,
            callback_headers=headers,
        )
    except InvalidCallbackError as exc:
        logger.warning(
            "callback.signature_invalid",
            extra={"provider": provider_name.value, "reason": str(exc)},
        )
        async with tenant_connection(state.app_pool, session_row["tenant_id"]) as conn:
            await repo.transition_session(
                conn,
                session_id=session_row["id"],
                expected_from="verifying",
                to="failed",
                failure_reason="callback_signature_invalid",
            )
        await state.audit_writer.write_event(
            tenant_id=session_row["tenant_id"],
            kind=audit_kinds.SIGNING_CALLBACK_SIGNATURE_INVALID,
            actor_sub=None,
            actor_role="provider",
            target_kind="signing_session",
            target_id=session_row["id"],
            payload={"provider": provider_name.value, "reason": str(exc)[:200]},
            severity=Severity.SEC,
        )
        return _no_info_response(401)

    # 3) Verify envelope against trust store.
    parsed = Envelope(envelope.signed_bytes,
                      declared_format=EnvelopeFormat(envelope.parsed.format)).parse()
    result = verify_envelope(
        parsed=parsed,
        expected_document_hash=envelope.parsed.document_hash_sha256,
        trust_store=state.trust_store,
    )
    if not result.valid:
        async with tenant_connection(state.app_pool, session_row["tenant_id"]) as conn:
            await repo.transition_session(
                conn,
                session_id=session_row["id"],
                expected_from="verifying",
                to="failed",
                failure_reason="envelope_verification_failed:" + ",".join(result.errors)[:200],
            )
        await state.audit_writer.write_event(
            tenant_id=session_row["tenant_id"],
            kind=audit_kinds.SIGNING_SESSION_FAILED,
            actor_sub=None,
            actor_role="provider",
            target_kind="signing_session",
            target_id=session_row["id"],
            payload={"errors": result.errors[:10]},
            severity=Severity.SEC,
        )
        return _no_info_response(400)

    # 4) Persist envelope.
    ipn = envelope.parsed.signer_ipn
    ipn_hmac_bytes = ipn_hmac(ipn, settings.signer_ipn_hmac_key_hex) if ipn else None
    token = new_verification_token()
    async with tenant_connection(state.app_pool, session_row["tenant_id"]) as conn:
        envelope_id = await repo.insert_envelope(
            conn,
            tenant_id=session_row["tenant_id"],
            signer_user_id=session_row["initiated_by"],
            resource_type=session_row["resource_type"],
            resource_id=session_row["resource_id"],
            resource_version_id=session_row["resource_version_id"],
            provider=provider_name,
            provider_session_id=session_id_str,
            provider_envelope_id=envelope.provider_envelope_id,
            canonical_json={},  # filled in by signing-service caller path; sprint-09 wire is via internal API only
            canonical_json_hash=envelope.parsed.document_hash_sha256,
            signed_at=envelope.parsed.signed_at,
            signed_data=envelope.signed_bytes,
            signature_algorithm=envelope.parsed.signature_algorithm,
            verification_token=token,
            pdf_storage_uri=None,
            signer_ipn_hmac=ipn_hmac_bytes,
            signer_full_name=envelope.parsed.signer_full_name or "",
            certificate_serial=envelope.parsed.signer_cert_serial,
            certificate_issuer_cn=envelope.parsed.signer_cert_issuer_cn,
            certificate_chain=envelope.parsed.cert_chain_pem,
            tsa_response=None,
            ocsp_responses=[],
            is_qualified=envelope.parsed.is_qualified,
            ltv_enabled=envelope.parsed.tsa_token_present and envelope.parsed.ocsp_responses_present,
        )
        await repo.transition_session(
            conn,
            session_id=session_row["id"],
            expected_from="verifying",
            to="signed",
            signed_envelope_id=envelope_id,
        )

    await state.audit_writer.write_event(
        tenant_id=session_row["tenant_id"],
        kind=audit_kinds.SIGNING_ENVELOPE_PERSISTED,
        actor_sub=session_row["initiated_by"],
        actor_role="clinician",
        target_kind="signed_envelope",
        target_id=envelope_id,
        payload={
            "provider": provider_name.value,
            "verification_token": token,
            "is_qualified": envelope.parsed.is_qualified,
        },
        severity=Severity.INFO,
    )

    return {"status": "ok", "verification_token": token}


def _no_info_response(status_code: int) -> dict[str, str]:
    """Returns a body that leaks nothing. The actual HTTP status is
    set by FastAPI via the inferred return type. For sprint-09 we
    return a generic body and let the caller infer status via test
    instrumentation; HTTPException is used for explicit codes."""
    from fastapi import HTTPException as _H

    raise _H(status_code=status_code, detail="")
