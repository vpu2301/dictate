"""Public /verify endpoint — unauthenticated, rate-limited."""

from __future__ import annotations

import hashlib
import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Request, Response, status

from .. import audit_kinds
from .. import repository as repo
from ..config import settings
from ..deps import get_state
from ..security import ip_hmac, is_well_formed_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/verify", tags=["public-verify"])


@router.get("/{token}")
async def verify(
    token: Annotated[str, Path(min_length=1, max_length=64)],
    request: Request,
) -> dict[str, object]:
    state = get_state()
    if not is_well_formed_token(token):
        await _audit_public(token, request, "not_found", 0, state)
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not found")

    ip = _client_ip(request)
    allowed, retry = await state.rate_limiter.check(ip=ip)
    if not allowed:
        await _audit_public(token, request, "rate_limited", 0, state)
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": str(retry)},
            detail="rate limited",
        )

    async with state.public_verify_pool.acquire() as conn:
        row = await repo.fetch_envelope_by_token(conn, token=token)
    if row is None:
        await _audit_public(token, request, "not_found", 0, state)
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not found")

    # Build the public response. Deliberately small surface.
    body: dict[str, object] = {
        "resource_type": row["resource_type"],
        "signed_at": row["signed_at"].isoformat(),
        "signer_full_name": row["signer_full_name"],
        "is_qualified": bool(row["is_qualified"]),
        "certificate_issuer_cn": row["certificate_issuer_cn"],
        "certificate_serial": row["certificate_serial"],
        "signature_algorithm": row["signature_algorithm"],
        "document_hash_sha256_hex": bytes(row["canonical_json_hash"]).hex(),
        "valid": True,
        "verification_token": token,
    }
    await _audit_public(token, request, "valid", _approx_size(body), state)
    return body


@router.get("/{token}/pdf")
async def verify_pdf(
    token: Annotated[str, Path(min_length=1, max_length=64)],
    request: Request,
) -> Response:
    state = get_state()
    if not is_well_formed_token(token):
        await _audit_public(token, request, "not_found", 0, state)
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not found")

    ip = _client_ip(request)
    allowed, retry = await state.rate_limiter.check(ip=ip)
    if not allowed:
        await _audit_public(token, request, "rate_limited", 0, state)
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": str(retry)},
            detail="rate limited",
        )

    async with state.public_verify_pool.acquire() as conn:
        row = await repo.fetch_envelope_by_token(conn, token=token)
    if row is None:
        await _audit_public(token, request, "not_found", 0, state)
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not found")

    pdf_bytes = bytes(row["signed_data"]) if row["signed_data"] else b""
    safe_name = _sanitize_filename(row["resource_type"] + "-" + token) + ".pdf"
    headers = {
        "Content-Disposition": f'attachment; filename="{safe_name}"',
    }
    await _audit_public(token, request, "valid", len(pdf_bytes), state,
                        kind=audit_kinds.PUBLIC_VERIFY_PDF_FETCH)
    return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)


# ── Helpers ─────────────────────────────────────────────────────────


def _client_ip(request: Request) -> str:
    """Trust the first hop in X-Forwarded-For; fall back to client.host."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "0.0.0.0"


def _sanitize_filename(name: str) -> str:
    safe = "".join(ch for ch in name if ch.isalnum() or ch in "-_")
    return safe[:64] or "report"


def _approx_size(obj: dict[str, object]) -> int:
    import json

    return len(json.dumps(obj, ensure_ascii=False))


async def _audit_public(
    token: str,
    request: Request,
    result: str,
    bytes_returned: int,
    state,
    kind: str = audit_kinds.PUBLIC_VERIFY_LOOKUP,
) -> None:
    ip_h = ip_hmac(_client_ip(request), settings.public_verify_ip_hmac_key_hex)
    ua = request.headers.get("user-agent", "")
    ua_h = hashlib.sha256(ua.encode("utf-8")).digest() if ua else None
    try:
        async with state.audit_writer_pool.acquire() as conn:
            await repo.insert_public_verify_audit(
                conn,
                event_kind=kind,
                verification_token=token[:64],
                requestor_ip_hmac=ip_h,
                user_agent_hash=ua_h,
                result=result,
                bytes_returned=bytes_returned,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("public_verify.audit_write_failed: %s", exc)
