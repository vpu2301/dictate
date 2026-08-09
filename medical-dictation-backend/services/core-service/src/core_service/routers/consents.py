"""Per-patient consents (AI-scribe / data-processing) with withdrawal.

S11 step 03 adds the strongest attestation tier: a consent captured with
``method='digital'`` is signable with КЕП through the S09 signing stack
(``resource_type='consent'``). Core-service builds the canonical consent
document and proxies to signing-service exactly like report-service does
for reports; signing-service writes ``patient_consents.signed_envelope_id``
in the same transaction that persists the envelope (the one linking
pattern in the codebase — see ``signing_service.repository.
mark_resource_signed``).

Consents are not versioned: ``resource_version_id == consent id`` by
convention (documented in docs/architecture/signing.md).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

import asyncpg
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from auth import Claims
from db import tenant_connection

from .. import audit_helper, audit_kinds
from ..config import settings
from ..deps import get_state, requires
from ..domain import consents_repository, patients_repository
from ..domain.consent_canonical import canonical_consent
from ..domain.consent_texts import consent_text_sha256
from ._phi_access_guard import PatientAccess, patient_record_access

logger = logging.getLogger(__name__)

router = APIRouter(tags=["consents"])

_SIGNING_TIMEOUT = 30.0


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConsentCreate(_Strict):
    type: str = "ai_scribe"
    method: str = "verbal"
    version: str = ""
    encounter_id: UUID | None = None
    status: str = "granted"  # accepted for FE symmetry; new rows are 'granted'


class ConsentOut(_Strict):
    id: UUID
    patient_id: UUID
    encounter_id: UUID | None
    type: str
    method: str
    version: str
    status: str
    granted_at: datetime
    withdrawn_at: datetime | None
    # S11 step 03: КЕП envelope linkage (digital consents only).
    signed_envelope_id: UUID | None = None


class ConsentSigningHint(_Strict):
    """What the FE needs to open the standard S09 sign dialog."""

    resource_type: Literal["consent"] = "consent"
    resource_id: UUID
    resource_version_id: UUID  # == consent id; consents are unversioned
    canonical_hash_hex: str


class ConsentCreated(ConsentOut):
    signing: ConsentSigningHint | None = None


class ConsentSignRequest(_Strict):
    provider: Literal["file_key", "dev_password", "diia"]
    # file_key credentials
    key_container_b64: str | None = None
    key_password: str | None = None
    # dev_password credential
    password: str | None = None


class ConsentSignedResponse(_Strict):
    consent: ConsentOut
    envelope_id: UUID
    signature_level: str
    verification_token: str
    signed_at: str
    signer_full_name: str
    is_qualified: bool


def _to_out(row: asyncpg.Record) -> ConsentOut:
    return ConsentOut(
        id=row["id"],
        patient_id=row["patient_id"],
        encounter_id=row["encounter_id"],
        type=row["type"],
        method=row["method"],
        version=row["version"],
        status=row["status"],
        granted_at=row["granted_at"],
        withdrawn_at=row["withdrawn_at"],
        signed_envelope_id=row.get("signed_envelope_id"),
    )


def _http_error(status_code: int, detail: str, **extras: object) -> HTTPException:
    exc = HTTPException(status_code=status_code, detail=detail)
    exc.problem_extras = extras  # type: ignore[attr-defined]
    return exc


# ── Canonical rebuild (shared by create + sign) ─────────────────────


async def _build_canonical(
    conn: asyncpg.Connection,
    *,
    consent_id: UUID,
    tenant_id: UUID,
    patient_id: UUID,
    type_: str,
    version: str,
    granted_at: datetime,
    attested_by: UUID,
) -> tuple[dict, bytes, str]:
    """Canonical doc from current row state. 422 when the (type, version)
    text is not in the approved set; 404 when the patient vanished."""
    text_sha = consent_text_sha256(type_, version)
    if text_sha is None:
        raise _http_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"no approved consent text for type={type_!r} version={version!r}",
            code="consent_text_version_unknown",
        )
    patient = await patients_repository.get_patient(conn, patient_id=patient_id)
    if patient is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    display_name = (
        await consents_repository.fetch_user_display_name(conn, sub=attested_by) or ""
    )
    return canonical_consent(
        consent_id=consent_id,
        tenant_id=tenant_id,
        patient_id=patient_id,
        patient_name_uk=patient["name_uk"],
        consent_type=type_,
        text_version=version,
        text_sha256=text_sha,
        granted_at=granted_at,
        attested_by_sub=attested_by,
        attested_by_name=display_name,
    )


async def _post_signing(
    path: str, body: dict, *, auth_header: str, expected: tuple[int, ...]
) -> dict:
    """Proxy to signing-service, mapping its errors 1:1 (report pattern)."""
    url = settings.signing_service_base_url.rstrip("/") + path
    try:
        async with httpx.AsyncClient(timeout=_SIGNING_TIMEOUT) as client:
            resp = await client.post(url, json=body, headers={"Authorization": auth_header})
    except httpx.HTTPError as exc:
        logger.warning("consent_sign.signing_service_unreachable: %s", exc.__class__.__name__)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "signing_service_unavailable"},
        ) from exc

    if resp.status_code in expected:
        return resp.json()
    try:
        detail = resp.json().get("detail", resp.json())
    except Exception:  # noqa: BLE001
        detail = {"error": "signing_failed"}
    raise HTTPException(resp.status_code, detail=detail)


# ── Routes ──────────────────────────────────────────────────────────


@router.get(
    "/patients/{patient_id}/consents",
    response_model=list[ConsentOut],
    summary="Consent history for a patient.",
)
async def list_consents(
    patient_id: UUID,
    # `patient_record_access`, not `patient.read` (2026-08-09). `patient.read`
    # is the ROSTER permission — an administrator holds it so they can find a
    # patient to break glass on, and it returns them redacted rows. This
    # endpoint returns no such thing: a consent names the patient, the clinical
    # act they agreed to, how they agreed to it and which clinician attested
    # it. That is the record, and every other part of the record — timeline,
    # encounters, anamnesis, documents — is behind this guard. Consents were
    # the one door left ajar, so an admin could read a patient's consent
    # history without a grant, a reason, or a `sec` audit event.
    access: Annotated[PatientAccess, Depends(patient_record_access)],
) -> list[ConsentOut]:
    claims = access.claims
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        rows = await consents_repository.list_for_patient(conn, patient_id=patient_id)
    return [_to_out(r) for r in rows]


@router.post(
    "/patients/{patient_id}/consents",
    response_model=ConsentCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Record a consent (digital consents become signable).",
)
async def create_consent(
    patient_id: UUID,
    body: ConsentCreate,
    claims: Annotated[Claims, Depends(requires("patient.write", "patient"))],
) -> ConsentCreated:
    state = get_state()
    consent_id = uuid4()
    granted_at = datetime.now(UTC)
    canonical_hash: bytes | None = None
    hint: ConsentSigningHint | None = None

    async with tenant_connection(state.app_pool, claims.tid) as conn:
        if await patients_repository.get_patient(conn, patient_id=patient_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        if body.method == "digital":
            # The canonical document binds the approved wording; unknown
            # (type, version) pairs are rejected before the row exists.
            _, _, hash_hex = await _build_canonical(
                conn,
                consent_id=consent_id,
                tenant_id=claims.tid,
                patient_id=patient_id,
                type_=body.type,
                version=body.version,
                granted_at=granted_at,
                attested_by=claims.sub,
            )
            canonical_hash = bytes.fromhex(hash_hex)
            hint = ConsentSigningHint(
                resource_id=consent_id,
                resource_version_id=consent_id,
                canonical_hash_hex=hash_hex,
            )
        row = await consents_repository.create_consent(
            conn,
            consent_id=consent_id,
            tenant_id=claims.tid,
            patient_id=patient_id,
            encounter_id=body.encounter_id,
            created_by=claims.sub,
            type_=body.type,
            method=body.method,
            version=body.version,
            granted_at=granted_at,
            canonical_hash=canonical_hash,
        )
    await audit_helper.emit(
        state,
        claims,
        audit_kinds.CONSENT_GRANTED,
        target_kind="patient",
        target_id=patient_id,
        payload={"consent_id": str(row["id"]), "type": body.type, "method": body.method},
    )
    out = _to_out(row)
    return ConsentCreated(**out.model_dump(), signing=hint)


@router.post(
    "/patients/{patient_id}/consents/{consent_id}/sign",
    summary="Sign a digital consent with КЕП via the S09 signing stack.",
)
async def sign_consent(
    patient_id: UUID,
    consent_id: UUID,
    body: ConsentSignRequest,
    request: Request,
    # HOTFIX — applying a КЕП to a consent is a clinical attestation, not
    # patient-record bookkeeping. `patient.write` is held by BOTH nurse
    # and tenant_admin, so this route let an operational admin sign a
    # clinical consent. Recording that a consent exists (POST /consents)
    # keeps `patient.write`; signing it is clinician-only.
    claims: Annotated[Claims, Depends(requires("consent.sign", "consent"))],
):
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        row = await consents_repository.get_consent(
            conn, consent_id=consent_id, patient_id=patient_id
        )
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        if row["method"] != "digital":
            raise _http_error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "only method='digital' consents are signable",
                code="consent_not_digital",
            )
        if row["signed_envelope_id"] is not None:
            raise _http_error(
                status.HTTP_409_CONFLICT,
                "this consent already carries a signed envelope",
                code="consent_already_signed",
            )
        canonical_json, _, hash_hex = await _build_canonical(
            conn,
            consent_id=consent_id,
            tenant_id=claims.tid,
            patient_id=patient_id,
            type_=row["type"],
            version=row["version"],
            granted_at=row["granted_at"],
            attested_by=row["created_by"],
        )
        if row["canonical_hash"] is None or row["canonical_hash"].hex() != hash_hex:
            # Patient/clinician display data (or the row) drifted since
            # capture — the document the signature would bind no longer
            # matches what was attested. Re-capture the consent.
            raise _http_error(
                status.HTTP_409_CONFLICT,
                "consent canonical document changed since capture; re-capture it",
                code="consent_canonical_changed",
            )

    auth_header = request.headers.get("authorization", "")

    if body.provider == "diia":
        session_body = {
            "resource_type": "consent",
            "resource_id": str(consent_id),
            "resource_version_id": str(consent_id),
            "document_pdf_hash_hex": hash_hex,
            "display": {
                "title": f"Згода пацієнта ({row['type']})",
                "code": f"CONSENT-{str(consent_id)[:8]}",
                "issuer": "",
                "page_count": 1,
                "language": "uk",
            },
            "user_provider_choice": "diia",
            "canonical_json": canonical_json,
        }
        result = await _post_signing(
            "/signing/sessions", session_body, auth_header=auth_header, expected=(200,)
        )
        return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=result)

    inline_body: dict[str, object] = {
        "resource_type": "consent",
        "resource_id": str(consent_id),
        "resource_version_id": str(consent_id),
        "provider": body.provider,
        "canonical_json": canonical_json,
        "canonical_hash_hex": hash_hex,
    }
    if body.provider == "file_key":
        if not body.key_container_b64 or not body.key_password:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "missing_credentials",
                    "detail": "file_key requires key_container_b64 and key_password",
                },
            )
        inline_body["key_container_b64"] = body.key_container_b64
        inline_body["key_password"] = body.key_password
    else:  # dev_password
        if not body.password:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "missing_credentials",
                    "detail": "dev_password requires password",
                },
            )
        inline_body["password"] = body.password

    result = await _post_signing(
        "/signing/inline", inline_body, auth_header=auth_header, expected=(200,)
    )

    # signing-service linked the envelope in its persist transaction —
    # re-read the row and audit the completed linkage.
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        row = await consents_repository.get_consent(
            conn, consent_id=consent_id, patient_id=patient_id
        )
    assert row is not None and row["signed_envelope_id"] is not None
    await audit_helper.emit(
        state,
        claims,
        audit_kinds.CONSENT_SIGNED,
        target_kind="patient",
        target_id=patient_id,
        payload={
            "consent_id": str(consent_id),
            "envelope_id": str(result["envelope_id"]),
            "signature_level": result["signature_level"],
            "is_qualified": result["is_qualified"],
        },
    )
    return ConsentSignedResponse(
        consent=_to_out(row),
        envelope_id=result["envelope_id"],
        signature_level=result["signature_level"],
        verification_token=result["verification_token"],
        signed_at=result["signed_at"],
        signer_full_name=result["signer_full_name"],
        is_qualified=result["is_qualified"],
    )


@router.post(
    "/patients/{patient_id}/consents/{consent_id}/withdraw",
    response_model=ConsentOut,
    summary="Withdraw a previously-granted consent.",
)
async def withdraw_consent(
    patient_id: UUID,
    consent_id: UUID,
    claims: Annotated[Claims, Depends(requires("patient.write", "patient"))],
) -> ConsentOut:
    """Withdrawal is always allowed, signed or not: a signed-then-withdrawn
    consent keeps its envelope (the grant is a historical fact) and gains
    ``withdrawn_at`` — the timeline and DSAR must show both facts."""
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        row = await consents_repository.withdraw_consent(
            conn, consent_id=consent_id, patient_id=patient_id, when=datetime.now(UTC)
        )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="consent not found or already withdrawn",
        )
    await audit_helper.emit(
        state,
        claims,
        audit_kinds.CONSENT_WITHDRAWN,
        target_kind="patient",
        target_id=patient_id,
        payload={"consent_id": str(consent_id)},
    )
    return _to_out(row)
