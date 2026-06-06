"""Repository — thin SQL wrapper over signed_envelopes + signing_sessions
+ signing_provider_health + public_verify_audit.

All tenant-scoped queries take a tenant-bound asyncpg.Connection
(opened via ``db.tenant_connection``). The public verify lookup uses a
separate connection on the ``app_public_verify`` role which sees rows
regardless of tenant.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg

from medical_kep import ProviderName

logger = logging.getLogger(__name__)


# ── signing_sessions ────────────────────────────────────────────────


async def insert_session(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    initiated_by: UUID,
    resource_type: str,
    resource_id: UUID,
    resource_version_id: UUID,
    provider: ProviderName,
    provider_session_id: str,
    expires_at: datetime,
    redirect_url: str | None,
    qr_payload: str | None,
    callback_completion_url: str | None,
    purpose_code: str | None,
) -> UUID:
    return await conn.fetchval(
        """
        INSERT INTO signing_sessions
            (tenant_id, initiated_by, resource_type, resource_id,
             resource_version_id, provider, provider_session_id,
             status, expires_at, redirect_url, qr_payload,
             callback_completion_url, purpose_code)
        VALUES ($1, $2, $3, $4, $5, $6::signing_provider, $7,
                'awaiting_user', $8, $9, $10, $11, $12)
        RETURNING id
        """,
        tenant_id, initiated_by, resource_type, resource_id,
        resource_version_id, provider.value, provider_session_id,
        expires_at, redirect_url, qr_payload, callback_completion_url, purpose_code,
    )


async def fetch_session_by_provider_id(
    conn: asyncpg.Connection,
    *,
    provider: ProviderName,
    provider_session_id: str,
) -> asyncpg.Record | None:
    return await conn.fetchrow(
        """
        SELECT id, tenant_id, initiated_by, resource_type, resource_id,
               resource_version_id, status, expires_at, signed_envelope_id,
               provider, provider_session_id
        FROM signing_sessions
        WHERE provider = $1::signing_provider AND provider_session_id = $2
        """,
        provider.value, provider_session_id,
    )


async def transition_session(
    conn: asyncpg.Connection,
    *,
    session_id: UUID,
    expected_from: str,
    to: str,
    failure_reason: str | None = None,
    signed_envelope_id: UUID | None = None,
) -> bool:
    row = await conn.fetchrow(
        """
        UPDATE signing_sessions
        SET status            = $3::signing_session_status,
            last_state_change = now(),
            failure_reason    = COALESCE($4, failure_reason),
            signed_envelope_id = COALESCE($5, signed_envelope_id)
        WHERE id = $1 AND status = $2::signing_session_status
        RETURNING id
        """,
        session_id, expected_from, to, failure_reason, signed_envelope_id,
    )
    return row is not None


async def expire_due_sessions(
    conn: asyncpg.Connection, *, grace_seconds: int = 60
) -> list[UUID]:
    rows = await conn.fetch(
        """
        UPDATE signing_sessions
        SET status            = 'expired',
            last_state_change = now(),
            failure_reason    = 'reaper_ttl'
        WHERE status IN ('initiating', 'awaiting_user')
          AND expires_at < now() - ($1 || ' seconds')::interval
        RETURNING id
        """,
        str(grace_seconds),
    )
    return [r["id"] for r in rows]


async def mark_stuck_verifying(
    conn: asyncpg.Connection, *, stuck_minutes: int = 5
) -> list[UUID]:
    rows = await conn.fetch(
        """
        UPDATE signing_sessions
        SET status            = 'failed',
            last_state_change = now(),
            failure_reason    = 'verification_stuck'
        WHERE status            = 'verifying'
          AND last_state_change < now() - ($1 || ' minutes')::interval
        RETURNING id
        """,
        str(stuck_minutes),
    )
    return [r["id"] for r in rows]


# ── signed_envelopes ────────────────────────────────────────────────


async def insert_envelope(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    signer_user_id: UUID,
    resource_type: str,
    resource_id: UUID,
    resource_version_id: UUID,
    provider: ProviderName,
    provider_session_id: str,
    provider_envelope_id: str,
    canonical_json: dict[str, Any],
    canonical_json_hash: bytes,
    signed_at: datetime,
    signed_data: bytes,
    signature_algorithm: str,
    verification_token: str,
    pdf_storage_uri: str | None,
    signer_ipn_hmac: bytes | None,
    signer_full_name: str,
    certificate_serial: str,
    certificate_issuer_cn: str,
    certificate_chain: list[str],
    tsa_response: bytes | None,
    ocsp_responses: list[bytes],
    is_qualified: bool,
    ltv_enabled: bool,
) -> UUID:
    return await conn.fetchval(
        """
        INSERT INTO signed_envelopes (
            tenant_id, signer_user_id, resource_type, resource_id, resource_version_id,
            provider, provider_session_id, provider_envelope_id,
            canonical_json, canonical_json_hash,
            signed_at, signed_data, signature_algorithm,
            verification_token, pdf_storage_uri,
            signer_ipn_hmac, signer_full_name,
            certificate_serial, certificate_issuer_cn, certificate_chain,
            tsa_response, ocsp_responses, is_qualified, ltv_enabled
        )
        VALUES (
            $1, $2, $3, $4, $5,
            $6::signing_provider, $7, $8,
            $9::jsonb, $10,
            $11, $12, $13,
            $14, $15,
            $16, $17,
            $18, $19, $20::text[],
            $21, $22::bytea[], $23, $24
        )
        RETURNING id
        """,
        tenant_id, signer_user_id, resource_type, resource_id, resource_version_id,
        provider.value, provider_session_id, provider_envelope_id,
        json.dumps(canonical_json), canonical_json_hash,
        signed_at, signed_data, signature_algorithm,
        verification_token, pdf_storage_uri,
        signer_ipn_hmac, signer_full_name,
        certificate_serial, certificate_issuer_cn, certificate_chain,
        tsa_response, ocsp_responses, is_qualified, ltv_enabled,
    )


async def fetch_envelope_by_token(
    conn: asyncpg.Connection, *, token: str
) -> asyncpg.Record | None:
    """Public verify lookup. Runs on the ``app_public_verify`` role
    pool with a stripped column set (RLS policy enforces visibility)."""
    return await conn.fetchrow(
        """
        SELECT id, resource_type, signed_at, verification_token,
               pdf_storage_uri, signer_full_name,
               certificate_serial, certificate_issuer_cn,
               is_qualified, signature_algorithm,
               canonical_json_hash, signed_data
        FROM signed_envelopes
        WHERE verification_token = $1
        """,
        token,
    )


# ── signing_provider_health ────────────────────────────────────────


async def upsert_provider_health(
    conn: asyncpg.Connection,
    *,
    provider: ProviderName,
    healthy: bool,
    last_error: str | None,
) -> bool:
    """Returns True if the healthy flag flipped."""
    row = await conn.fetchrow(
        "SELECT healthy, consecutive_failures FROM signing_provider_health WHERE provider=$1::signing_provider",
        provider.value,
    )
    if row is None:
        await conn.execute(
            """
            INSERT INTO signing_provider_health
                (provider, healthy, last_check_at, consecutive_failures, last_error)
            VALUES ($1::signing_provider, $2, now(), $3, $4)
            """,
            provider.value, healthy, 0 if healthy else 1, last_error,
        )
        return True
    prev_healthy = bool(row["healthy"])
    prev_fail = int(row["consecutive_failures"])
    new_fail = 0 if healthy else prev_fail + 1
    # 3-strike rule: flip to unhealthy only after 3 consecutive
    # failures; flip to healthy on first success.
    new_healthy = healthy or (prev_healthy and new_fail < 3)
    await conn.execute(
        """
        UPDATE signing_provider_health
        SET healthy              = $2,
            last_check_at        = now(),
            consecutive_failures = $3,
            last_error           = $4
        WHERE provider = $1::signing_provider
        """,
        provider.value, new_healthy, new_fail, last_error,
    )
    return new_healthy != prev_healthy


async def fetch_all_provider_health(conn: asyncpg.Connection) -> dict[str, bool]:
    rows = await conn.fetch("SELECT provider, healthy FROM signing_provider_health")
    return {str(r["provider"]): bool(r["healthy"]) for r in rows}


# ── public_verify_audit ─────────────────────────────────────────────


async def insert_public_verify_audit(
    conn: asyncpg.Connection,
    *,
    event_kind: str,
    verification_token: str,
    requestor_ip_hmac: bytes,
    user_agent_hash: bytes | None,
    result: str,
    bytes_returned: int,
) -> None:
    await conn.execute(
        """
        INSERT INTO audit.public_verify_audit
            (event_kind, verification_token, requestor_ip_hmac,
             user_agent_hash, result, bytes_returned)
        VALUES ($1, $2, $3, $4, $5, $6)
        """,
        event_kind, verification_token, requestor_ip_hmac,
        user_agent_hash, result, bytes_returned,
    )
