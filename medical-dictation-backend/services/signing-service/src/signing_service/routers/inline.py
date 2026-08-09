"""POST /signing/inline — synchronous signing inside one request.

Serves the two inline providers of the S09 revision:

- ``file_key`` (qualified) — the clinician's uploaded КНЕДП file-key
  container + password sign the canonical bytes server-side via the
  UAPKI backend. The container lives in memory only for the duration
  of the request.
- ``dev_password`` (dev tier) — account-password confirmation scaffold;
  development builds only (the provider simply isn't wired otherwise).

The route re-canonicalises the supplied canonical JSON (RFC 8785) and
requires the caller's hash to match — the envelope is bound to bytes
this service derived, never to a caller-asserted hash. Qualified
envelopes are verified against the trust store BEFORE persisting
(``is_qualified`` persists the *verifier's* verdict, so test-CA chains
can never be recorded as qualified).
"""

from __future__ import annotations

import base64
import hashlib
import logging
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from medical_kep import (
    AccountLockedError,
    InlineCredentials,
    InvalidCredentialsError,
    ProviderName,
    ProviderTransientError,
    SignatureLevel,
    SignerIdentity,
    verify_envelope,
)
from medical_kep.envelope import Envelope, EnvelopeFormat, EnvelopeParseError
from pydantic import BaseModel, ConfigDict, Field

from audit import Severity
from auth import Claims
from db import tenant_connection
from secret import Secret

from .. import audit_kinds
from .. import repository as repo
from ..config import settings
from ..deps import get_state, requires
from ..security import ipn_hmac, new_verification_token
from ..signing_authority import assert_may_sign

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/signing", tags=["signing"])

_INLINE_PROVIDERS: dict[str, ProviderName] = {
    "file_key": ProviderName.FILE_KEY,
    "dev_password": ProviderName.DEV_PASSWORD,
}


class InlineSignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource_type: str = Field(pattern=r"^(report|amendment|consent)$")
    resource_id: UUID
    resource_version_id: UUID
    provider: Literal["file_key", "dev_password"]
    canonical_json: dict[str, Any]
    canonical_hash_hex: str = Field(min_length=64, max_length=64)
    # file_key credentials
    key_container_b64: str | None = None
    key_password: str | None = None
    # dev_password credential
    password: str | None = None
    # Optional pre-rendered artifact (dev tier: the watermarked PDF).
    artifact_pdf_b64: str | None = None
    purpose_code: str | None = None


class InlineSignResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    envelope_id: UUID
    session_id: UUID
    signature_level: str
    verification_token: str
    signed_at: str
    signer_full_name: str
    is_qualified: bool
    report_status: str | None = None


def _problem(code: int, error: str, detail: str) -> HTTPException:
    return HTTPException(code, detail={"error": error, "detail": detail})


@router.post("/inline", response_model=InlineSignResponse)
async def sign_inline(
    body: InlineSignRequest,
    # HOTFIX — the synchronous signing path (file_key/dev_password/mock).
    # This reaches the crypto directly; clinician-only.
    claims: Annotated[Claims, Depends(requires("report.sign", "report"))],
) -> InlineSignResponse:
    # Defence in depth before the synchronous signing path touches a
    # provider — this one reaches the crypto in the same request.
    await assert_may_sign(claims, resource_kind="report")

    state = get_state()
    provider_name = _INLINE_PROVIDERS[body.provider]

    try:
        signer_impl = state.providers.get_inline(provider_name)
    except ValueError:
        raise _problem(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "provider_unavailable",
            f"inline provider {body.provider!r} is not configured on this deployment",
        ) from None

    # ── Canonical binding: recompute the JCS bytes ourselves ─────────
    from audit.canonical import canonicalize

    canonical_bytes = canonicalize(body.canonical_json)
    canonical_hash = hashlib.sha256(canonical_bytes).digest()
    if canonical_hash.hex() != body.canonical_hash_hex.lower():
        raise _problem(
            status.HTTP_400_BAD_REQUEST,
            "canonical_hash_mismatch",
            "canonical_hash_hex does not match the RFC-8785 canonicalisation "
            "of canonical_json",
        )

    credentials = _build_credentials(body)
    artifact_pdf = (
        base64.b64decode(body.artifact_pdf_b64) if body.artifact_pdf_b64 else None
    )
    signer = SignerIdentity(
        sub=str(claims.sub),
        display_name=claims.name or claims.preferred_username or str(claims.sub),
        username=claims.preferred_username or claims.email,
    )

    # ── Session row (uniform lifecycle across all providers) ─────────
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        session_id = await repo.insert_session(
            conn,
            tenant_id=claims.tid,
            initiated_by=claims.sub,
            resource_type=body.resource_type,
            resource_id=body.resource_id,
            resource_version_id=body.resource_version_id,
            provider=provider_name,
            provider_session_id=f"inline-{new_verification_token()}",
            expires_at=_now_plus_seconds(120),
            redirect_url=None,
            qr_payload=None,
            callback_completion_url=None,
            purpose_code=body.purpose_code,
            document_pdf_hash=canonical_hash,
            canonical_json=body.canonical_json,
        )
        await repo.transition_session(
            conn, session_id=session_id, expected_from="awaiting_user", to="verifying"
        )

    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.SIGNING_SESSION_INITIATED,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="signing_session",
        target_id=session_id,
        payload={
            "provider": provider_name.value,
            "resource_type": body.resource_type,
            "resource_id": str(body.resource_id),
            "inline": True,
        },
        severity=Severity.INFO,
    )

    async def _fail(reason: str) -> None:
        async with tenant_connection(state.app_pool, claims.tid) as conn:
            await repo.transition_session(
                conn,
                session_id=session_id,
                expected_from="verifying",
                to="failed",
                failure_reason=reason[:200],
            )

    # ── Sign ─────────────────────────────────────────────────────────
    try:
        envelope = await signer_impl.sign_inline(
            canonical_bytes=canonical_bytes,
            credentials=credentials,
            signer=signer,
            artifact_pdf=artifact_pdf,
        )
    except InvalidCredentialsError as exc:
        await _fail(f"credentials_rejected:{exc}")
        if provider_name is ProviderName.DEV_PASSWORD:
            await _audit_reject(
                state, claims, session_id, audit_kinds.SIGNING_DEV_PASSWORD_REJECTED, exc
            )
            raise _problem(
                status.HTTP_401_UNAUTHORIZED,
                "account_password_rejected",
                "account password was not accepted",
            ) from exc
        await _audit_reject(
            state, claims, session_id, audit_kinds.SIGNING_FILE_KEY_REJECTED, exc
        )
        raise _problem(
            status.HTTP_400_BAD_REQUEST,
            "key_container_rejected",
            "key container could not be opened (bad container or wrong password)",
        ) from exc
    except AccountLockedError as exc:
        await _fail("account_locked")
        await _audit_reject(
            state, claims, session_id, audit_kinds.SIGNING_DEV_PASSWORD_REJECTED, exc
        )
        raise _problem(
            status.HTTP_423_LOCKED,
            "account_locked",
            "account is temporarily locked by the identity provider",
        ) from exc
    except ProviderTransientError as exc:
        await _fail(f"provider_transient:{exc}")
        raise _problem(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "provider_unavailable",
            "signing provider temporarily unavailable",
        ) from exc

    # ── Verify qualified envelopes against the trust store ──────────
    is_qualified = False
    if envelope.signature_level is SignatureLevel.QUALIFIED:
        try:
            parsed = Envelope(
                envelope.signed_bytes,
                declared_format=EnvelopeFormat(envelope.parsed.format),
            ).parse()
        except (EnvelopeParseError, ValueError) as exc:
            await _fail(f"envelope_parse_failed:{exc}")
            raise _problem(
                status.HTTP_502_BAD_GATEWAY,
                "envelope_invalid",
                "produced envelope failed structural validation",
            ) from exc
        result = verify_envelope(
            parsed=parsed,
            expected_document_hash=envelope.parsed.document_hash_sha256,
            trust_store=state.trust_store,
        )
        if not result.valid:
            await _fail("envelope_verification_failed:" + ",".join(result.errors))
            raise _problem(
                status.HTTP_502_BAD_GATEWAY,
                "envelope_invalid",
                "produced envelope failed verification: " + "; ".join(result.errors),
            )
        is_qualified = result.is_qualified

    # ── Persist + lifecycle in one transaction ───────────────────────
    ipn = envelope.parsed.signer_ipn
    ipn_hmac_bytes = ipn_hmac(ipn, settings.signer_ipn_hmac_key_hex) if ipn else None
    token = new_verification_token()
    try:
        async with tenant_connection(state.app_pool, claims.tid) as conn, conn.transaction():
            envelope_id = await repo.insert_envelope(
                conn,
                tenant_id=claims.tid,
                signer_user_id=claims.sub,
                resource_type=body.resource_type,
                resource_id=body.resource_id,
                resource_version_id=body.resource_version_id,
                provider=provider_name,
                provider_session_id=f"inline-{session_id}",
                provider_envelope_id=envelope.provider_envelope_id,
                canonical_json=body.canonical_json,
                canonical_json_hash=canonical_hash,
                signed_at=envelope.parsed.signed_at,
                signed_data=envelope.signed_bytes,
                signature_algorithm=envelope.parsed.signature_algorithm,
                verification_token=token,
                pdf_storage_uri=None,
                signer_ipn_hmac=ipn_hmac_bytes,
                signer_full_name=envelope.parsed.signer_full_name or signer.display_name,
                certificate_serial=envelope.parsed.signer_cert_serial,
                certificate_issuer_cn=envelope.parsed.signer_cert_issuer_cn,
                certificate_chain=envelope.parsed.cert_chain_pem,
                tsa_response=None,
                ocsp_responses=[],
                is_qualified=is_qualified,
                ltv_enabled=False,
                signature_level=envelope.signature_level.value,
            )
            report_status = await repo.mark_resource_signed(
                conn,
                resource_type=body.resource_type,
                resource_id=body.resource_id,
                resource_version_id=body.resource_version_id,
                signed_at=envelope.parsed.signed_at,
                signer_sub=claims.sub,
                envelope_id=envelope_id,
                canonical_bytes=canonical_bytes,
                canonical_hash=canonical_hash,
            )
            await repo.transition_session(
                conn,
                session_id=session_id,
                expected_from="verifying",
                to="signed",
                signed_envelope_id=envelope_id,
            )
    except repo.ResourceLinkError as exc:
        # Strict-link resources (consent, S11 step 03): the transaction
        # rolled back — no envelope was persisted. Surface the cause.
        raise _problem(
            status.HTTP_404_NOT_FOUND
            if exc.code == "consent_not_found"
            else status.HTTP_409_CONFLICT,
            exc.code,
            "the resource refused the envelope link; nothing was persisted",
        ) from exc

    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.SIGNING_ENVELOPE_PERSISTED,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="signed_envelope",
        target_id=envelope_id,
        payload={
            "provider": provider_name.value,
            "signature_level": envelope.signature_level.value,
            "is_qualified": is_qualified,
            "verification_token": token,
            "inline": True,
        },
        severity=Severity.INFO,
    )

    return InlineSignResponse(
        envelope_id=envelope_id,
        session_id=session_id,
        signature_level=envelope.signature_level.value,
        verification_token=token,
        signed_at=envelope.parsed.signed_at.isoformat(),
        signer_full_name=envelope.parsed.signer_full_name or signer.display_name,
        is_qualified=is_qualified,
        report_status=report_status,
    )


# ── Helpers ─────────────────────────────────────────────────────────


def _build_credentials(body: InlineSignRequest) -> InlineCredentials:
    if body.provider == "file_key":
        if not body.key_container_b64 or not body.key_password:
            raise _problem(
                status.HTTP_400_BAD_REQUEST,
                "missing_credentials",
                "file_key requires key_container_b64 and key_password",
            )
        try:
            container = base64.b64decode(body.key_container_b64, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise _problem(
                status.HTTP_400_BAD_REQUEST,
                "key_container_rejected",
                "key_container_b64 is not valid base64",
            ) from exc
        cap = settings.max_key_container_kb * 1024
        if len(container) > cap:
            raise _problem(
                status.HTTP_400_BAD_REQUEST,
                "key_container_rejected",
                f"key container exceeds {settings.max_key_container_kb} KiB",
            )
        return InlineCredentials(
            key_container=container, key_password=Secret(body.key_password)
        )
    if not body.password:
        raise _problem(
            status.HTTP_400_BAD_REQUEST,
            "missing_credentials",
            "dev_password requires password",
        )
    return InlineCredentials(dev_password=Secret(body.password))


def _now_plus_seconds(seconds: int):
    from datetime import UTC, datetime, timedelta

    return datetime.now(UTC) + timedelta(seconds=seconds)


async def _audit_reject(state, claims: Claims, session_id: UUID, kind: str, exc: Exception) -> None:
    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=kind,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="signing_session",
        target_id=session_id,
        payload={"reason": str(exc)[:200]},
        severity=Severity.SEC,
    )
