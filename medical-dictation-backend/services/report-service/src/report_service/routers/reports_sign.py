"""POST /v1/reports/{id}/sign — the one signing surface (S09 revision).

Replaces the sprint-08 ``501`` placeholder. The route owns everything a
signature legally binds to — it builds the RFC-8785 canonical bytes
from the report + current version and delegates envelope creation to
signing-service (the caller's JWT is forwarded; both services enforce
``report.write``):

- ``file_key``     → ``POST {signing}/signing/inline`` (qualified; the
                     clinician's own КНЕДП file key signs server-side)
- ``dev_password`` → ``POST {signing}/signing/inline`` (dev tier; the
                     watermarked artifact PDF is rendered here and
                     persisted as the envelope body)
- ``diia``         → ``POST {signing}/signing/sessions`` (202 +
                     redirect/QR; the callback completes the flow)

Status transitions (``finalized → signed``, amendment → ``amended``)
happen inside signing-service's persist transaction so the async Дія
path behaves identically to the inline ones.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Annotated, Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from medical_kep import canonicalize_report
from medical_kep.canonicalize import CanonicalReportInput
from pydantic import BaseModel, ConfigDict, Field

from audit import Severity
from auth import Claims
from db import tenant_connection
from notification_events import Category
from report_models import ReportStatus

from .. import audit_kinds
from ..config import settings
from ..deps import get_state, requires
from ..domain import reports_repository as repo
from ..domain.branding import load_tenant_branding
from ..domain.reports_repository import ReportRow, VersionRow
from ..notifications import emit_report_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/reports", tags=["reports"])

_SIGNING_TIMEOUT = httpx.Timeout(30.0, connect=5.0)


class SignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["file_key", "diia", "dev_password"]
    # file_key
    key_container_b64: str | None = Field(default=None, max_length=200_000)
    key_password: str | None = Field(default=None, max_length=512)
    # dev_password
    password: str | None = Field(default=None, max_length=512)
    language: str = "uk"


class SignInlineResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    envelope_id: UUID
    signature_level: str
    verification_token: str
    signed_at: str
    signer_full_name: str
    is_qualified: bool
    report_status: str


class SignSessionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: UUID
    provider: str
    expires_at: str
    redirect_url: str | None = None
    qr_payload: str | None = None


@router.post(
    "/{report_id}/sign",
    responses={
        200: {"model": SignInlineResponse},
        202: {"model": SignSessionResponse},
    },
)
async def sign_report(
    report_id: UUID,
    body: SignRequest,
    request: Request,
    # HOTFIX — signing is clinician-only. This used to gate on
    # `report.write`, which nurses hold: a nurse could apply a qualified
    # electronic signature to a clinical report. Preparing a report and
    # signing it are different authorities (see libs/auth/perms.py).
    claims: Annotated[Claims, Depends(requires("report.sign", "report"))],
):
    state = get_state()

    # ── Load + gate the report ────────────────────────────────────────
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        report = await repo.lock_report_for_update(conn, report_id=report_id)
        if report is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="report not found")
        version = await repo.fetch_version(conn, version_id=report.current_version_id)
        assert version is not None
        resource_type = _resource_type_for(report, version)
        branding = await load_tenant_branding(conn, tenant_id=claims.tid)

    # ── Canonical bytes: the legal contract ──────────────────────────
    canonical_bytes, canonical_hash_hex = canonicalize_report(
        _canonical_input(report, version, claims, branding.issuer_name)
    )
    canonical_json = json.loads(canonical_bytes)

    auth_header = request.headers.get("authorization", "")

    if body.provider == "diia":
        response = await _initiate_diia_session(
            report=report,
            version=version,
            resource_type=resource_type,
            canonical_json=canonical_json,
            canonical_hash_hex=canonical_hash_hex,
            auth_header=auth_header,
            issuer_name=branding.issuer_name,
            language=body.language,
        )
        await _audit_sign_requested(state, claims, report_id, body.provider, resource_type)
        return response

    # ── Inline providers ─────────────────────────────────────────────
    inline_body: dict[str, object] = {
        "resource_type": resource_type,
        "resource_id": str(report_id),
        "resource_version_id": str(version.id),
        "provider": body.provider,
        "canonical_json": canonical_json,
        "canonical_hash_hex": canonical_hash_hex,
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
        artifact = _render_dev_artifact(
            report=report,
            version=version,
            claims=claims,
            issuer_name=branding.issuer_name,
            canonical_bytes=canonical_bytes,
            signed_at_intent=str(canonical_json["lifecycle"]["signed_at_intent"]),
            language=body.language,
        )
        if artifact is not None:
            inline_body["artifact_pdf_b64"] = base64.b64encode(artifact).decode("ascii")

    result = await _post_signing(
        "/signing/inline", inline_body, auth_header=auth_header, expected=(200,)
    )
    await _audit_sign_requested(state, claims, report_id, body.provider, resource_type)

    # Sprint-12. An amendment that signs moves the report to `amended`,
    # so the category follows the resulting status rather than the verb.
    final_status = result.get("report_status") or (
        "amended" if resource_type == "amendment" else "signed"
    )
    await emit_report_event(
        state.redis,
        category=(
            Category.REPORT_AMENDED
            if final_status == "amended"
            else Category.REPORT_SIGNED
        ),
        tenant_id=claims.tid,
        report_id=report_id,
        report_code=report.code,
        actor_user_id=claims.sub,
        primary_author_id=report.primary_author_id,
        co_author_ids=tuple(report.co_author_ids),
        extra_payload={"signature_level": str(result.get("signature_level", ""))},
    )
    return SignInlineResponse(
        envelope_id=result["envelope_id"],
        signature_level=result["signature_level"],
        verification_token=result["verification_token"],
        signed_at=result["signed_at"],
        signer_full_name=result["signer_full_name"],
        is_qualified=result["is_qualified"],
        report_status=result.get("report_status")
        or ("amended" if resource_type == "amendment" else "signed"),
    )


# ── Helpers ─────────────────────────────────────────────────────────


def _resource_type_for(report: ReportRow, version: VersionRow) -> str:
    """A finalized report signs as 'report'; a signed/amended report
    whose current version is an unsigned amendment signs as
    'amendment'. Anything else is 409 (spec: report not finalized)."""
    if report.status == ReportStatus.FINALIZED:
        return "report"
    if (
        report.status in (ReportStatus.SIGNED, ReportStatus.AMENDED)
        and version.is_amendment
        and version.signed_at is None
    ):
        return "amendment"
    raise HTTPException(
        status.HTTP_409_CONFLICT,
        detail={
            "error": "report_not_signable",
            "current_status": report.status.value,
            "detail": "only a finalized report or a drafted amendment can be signed",
        },
    )


def _canonical_input(
    report: ReportRow, version: VersionRow, claims: Claims, issuer_name: str
) -> CanonicalReportInput:
    from datetime import UTC, datetime

    content = version.content
    author_is_signer = claims.sub == report.primary_author_id
    author_name = (
        (claims.name or claims.preferred_username or str(report.primary_author_id))
        if author_is_signer
        else str(report.primary_author_id)
    )
    encounter_date = content.encounter_date
    if encounter_date is None and report.encounter_date is not None:
        encounter_date = report.encounter_date.date().isoformat()
    return CanonicalReportInput(
        tenant_id=str(claims.tid),
        tenant_legal_name=issuer_name,
        report_id=str(report.id),
        report_code=report.code,
        report_version_id=str(version.id),
        report_version_number=version.version_number,
        title=report.title,
        encounter_date=encounter_date,
        primary_author_full_name=author_name,
        primary_author_id=str(report.primary_author_id),
        primary_author_role=(claims.roles[0] if author_is_signer and claims.roles else "clinician"),
        co_author_names=[str(a) for a in report.co_author_ids],
        patient_id=str(report.patient_id) if report.patient_id else None,
        patient_full_name_redacted=report.patient_name_redacted,
        icd10_codes=list(report.icd10_codes),
        sections=[
            {
                "section_key": s.section_key,
                "text": s.text,
                "transcript_segment_ids": [str(t) for t in s.transcript_segment_ids],
            }
            for s in content.sections
        ],
        template_id=str(content.template_id),
        template_schema_version=content.template_schema_version,
        finalized_at=report.finalized_at.isoformat() if report.finalized_at else "",
        signed_at_intent=datetime.now(UTC).isoformat(timespec="seconds"),
    )


def _render_dev_artifact(
    *,
    report: ReportRow,
    version: VersionRow,
    claims: Claims,
    issuer_name: str,
    canonical_bytes: bytes,
    signed_at_intent: str,
    language: str,
) -> bytes | None:
    """Watermarked dev artifact with the canonical JSON embedded.

    Best-effort: if the [pdf] extra's native deps are unavailable the
    dev envelope falls back to the canonical bytes themselves — the
    tier still reports truthfully everywhere.
    """
    try:
        from medical_kep.pdf_renderer import SignatureStamp, render_signed_pdf

        from ..domain.pdf import build_render_input

        render_input = build_render_input(
            report=report,
            version=version,
            issuer_name=issuer_name,
            is_draft=False,
            language=language,
        )
        return render_signed_pdf(
            render_input,
            stamp=SignatureStamp(
                level="dev",
                signer_full_name=claims.name or claims.preferred_username or str(claims.sub),
                provider_label="dev_password (development scaffold)",
                signed_at=signed_at_intent,
            ),
            canonical_bytes=canonical_bytes,
        )
    except Exception as exc:  # noqa: BLE001 — render is best-effort; the
        # dev tier stays truthful without an artifact (canonical bytes persist).
        logger.warning("sign.dev_artifact_render_unavailable: %s", exc)
        return None


async def _initiate_diia_session(
    *,
    report: ReportRow,
    version: VersionRow,
    resource_type: str,
    canonical_json: dict,
    canonical_hash_hex: str,
    auth_header: str,
    issuer_name: str,
    language: str,
) -> JSONResponse:
    session_body = {
        "resource_type": resource_type,
        "resource_id": str(report.id),
        "resource_version_id": str(version.id),
        "document_pdf_hash_hex": canonical_hash_hex,
        "display": {
            "title": report.title,
            "code": report.code,
            "issuer": issuer_name,
            "page_count": 1,
            "language": language,
        },
        "user_provider_choice": "diia",
        "canonical_json": canonical_json,
    }
    result = await _post_signing(
        "/signing/sessions", session_body, auth_header=auth_header, expected=(200,)
    )
    payload = SignSessionResponse(
        session_id=result["session_id"],
        provider=result["provider"],
        expires_at=result["expires_at"],
        redirect_url=result.get("redirect_url"),
        qr_payload=result.get("qr_payload"),
    )
    # 202: the signature is pending user action in the Дія app.
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content=json.loads(payload.model_dump_json()),
    )


async def _post_signing(
    path: str, body: dict, *, auth_header: str, expected: tuple[int, ...]
) -> dict:
    url = settings.signing_service_base_url.rstrip("/") + path
    try:
        async with httpx.AsyncClient(timeout=_SIGNING_TIMEOUT) as client:
            resp = await client.post(
                url, json=body, headers={"Authorization": auth_header}
            )
    except httpx.HTTPError as exc:
        logger.warning("sign.signing_service_unreachable: %s", exc.__class__.__name__)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "signing_service_unavailable"},
        ) from exc

    if resp.status_code in expected:
        return resp.json()

    # Pass the signing-service error through with its status code —
    # 400 bad container, 401 wrong account password, 423 locked,
    # 503 provider unhealthy all map 1:1 onto this surface.
    try:
        detail = resp.json().get("detail", resp.json())
    except Exception:  # noqa: BLE001
        detail = {"error": "signing_failed"}
    raise HTTPException(resp.status_code, detail=detail)


async def _audit_sign_requested(
    state, claims: Claims, report_id: UUID, provider: str, resource_type: str
) -> None:
    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.REPORT_SIGN_REQUESTED,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="report",
        target_id=report_id,
        payload={"provider": provider, "resource_type": resource_type},
        severity=Severity.INFO,
    )
