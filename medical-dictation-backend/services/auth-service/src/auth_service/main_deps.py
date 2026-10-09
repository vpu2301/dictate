"""Service-wide singletons: JWKS cache, DB pools, audit components.

Created at process start (in main.py's lifespan). Routers consume them
via :func:`auth_service.deps.get_state`.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import asyncpg

from audit import AuditVerifier, AuditWriter
from auth import JwksCache, RedisSessionDenylist, build_session_denylist
from crypto import Envelope, TenantKekRepository, build_master_key_provider
from db import create_pool

from .adapters.email import EmailProvider, build_provider
from .config import settings
from .jwks_metrics import instrument_jwks_cache
from .keycloak_client import KeycloakClient
from .rate_limit import PasswordResetRateLimiter


@dataclass
class ServiceState:
    """Container for runtime singletons. Stored on ``app.state.svc``."""

    jwks_cache: JwksCache
    app_pool: asyncpg.Pool
    tenant_writer_pool: asyncpg.Pool
    audit_writer_pool: asyncpg.Pool
    audit_reader_pool: asyncpg.Pool
    audit_writer: AuditWriter
    audit_verifier: AuditVerifier
    keycloak: KeycloakClient
    # ── Sprint 16: session-revocation denylist (None = feature off) ─────
    denylist: RedisSessionDenylist | None = None
    # ── Password recovery (None = feature off) ──────────────────────────
    email_provider: EmailProvider | None = None
    password_rate_limiter: PasswordResetRateLimiter | None = None
    _redis: Any = None

    @property
    def notification_bus(self) -> Any:
        """The Redis client S21's notification producer publishes on.

        None when notifications are off or the client failed to build —
        `emit_mfa_reminder` treats that as "don't publish", which is the
        correct posture: the reminder's durable half is the DB row.
        """
        return self._redis if settings.notifications_enabled else None

    # ── Sprint 16 MFA: lazy envelope wiring ──────────────────────────────
    # The TOTP secret store needs libs/crypto, which needs the master key
    # and the crypto_writer pool. Built on FIRST use so an auth-service
    # deployment that never enables MFA never needs the master key mounted.
    crypto_pool: asyncpg.Pool | None = None
    envelope: Envelope | None = None
    _envelope_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def get_envelope(self) -> Envelope:
        """Build (once) and return the envelope for TOTP-secret crypto.

        Raises ``crypto.MasterKeyError`` if the configured master-key
        provider is unusable — callers surface that as 503 with the
        runbook pointer, never as a silent fallback.
        """
        async with self._envelope_lock:
            if self.envelope is not None:
                return self.envelope
            master = build_master_key_provider(
                provider=settings.master_key_provider,
                file_path=settings.master_key_path,
                vault_addr=settings.vault_addr,
                vault_token=settings.vault_token,
                vault_transit_key=settings.vault_transit_key,
                vault_transit_mount=settings.vault_transit_mount,
            )
            await master.startup_self_check()
            self.crypto_pool = await create_pool(
                settings.db_crypto_writer_dsn,
                application_name=f"{settings.service_name}/crypto_writer",
                min_size=1,
                max_size=2,
            )
            kek_repo = TenantKekRepository(
                pool=self.crypto_pool, master_key_provider=master
            )
            self.envelope = Envelope(master_key_provider=master, kek_repository=kek_repo)
            return self.envelope


async def build_state() -> ServiceState:
    """Construct every async resource the service needs."""
    jwks_cache = JwksCache(issuer_to_url={settings.auth_issuer: settings.auth_jwks_url})
    instrument_jwks_cache(jwks_cache)

    app_pool = await create_pool(
        settings.db_app_role_dsn,
        application_name=f"{settings.service_name}/app",
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
    )
    tenant_writer_pool = await create_pool(
        settings.db_tenant_writer_dsn,
        application_name=f"{settings.service_name}/tenant_writer",
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
    )
    audit_writer_pool = await create_pool(
        settings.db_audit_writer_dsn,
        application_name=f"{settings.service_name}/audit_writer",
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
    )
    audit_reader_pool = await create_pool(
        settings.db_audit_reader_dsn,
        application_name=f"{settings.service_name}/audit_reader",
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
    )

    keycloak = KeycloakClient(
        base_url=settings.keycloak_base_url,
        realm=settings.keycloak_realm,
        login_client_id=settings.keycloak_login_client_id,
        login_client_secret=settings.keycloak_login_client_secret,
        admin_client_id=settings.keycloak_admin_client_id,
        admin_client_secret=settings.keycloak_admin_client_secret,
    )

    # ── Password recovery ────────────────────────────────────────────
    # Built only when the feature is on, so a deployment that never
    # enables it needs no mail relay, no Redis for the limiter, and
    # cannot trip the MockProvider production guard at startup.
    email_provider: EmailProvider | None = None
    password_rate_limiter: PasswordResetRateLimiter | None = None

    # One connection for both Redis users in this service — the recovery
    # rate limiter and (S21) the notification publisher. Built when EITHER
    # is on: a deployment that mails no password resets but does remind
    # users about MFA still needs a bus, and two clients to one server
    # would be two connection pools for no reason. (The revocation
    # denylist keeps its own client: it is built by libs/auth behind
    # `build_session_denylist` and has its own failure posture.)
    redis_client: Any = None
    if settings.password_reset_enabled or settings.notifications_enabled:
        try:
            import redis.asyncio as aioredis

            redis_client = aioredis.from_url(settings.redis_url, decode_responses=False)
        except Exception:  # noqa: BLE001 — both users are fail-open
            logging.getLogger(__name__).warning("auth.redis_unavailable_fail_open")

    if settings.password_reset_enabled:
        email_provider = build_provider(
            kind=settings.email_provider,
            is_production=settings.is_production,
            host=settings.auth_smtp_host,
            port=settings.auth_smtp_port,
            from_address=settings.auth_email_from,
            from_name=settings.auth_email_from_name,
            use_tls=settings.auth_smtp_use_tls,
            username=settings.auth_smtp_username,
            password=settings.auth_smtp_password.value(),
        )
        try:
            if redis_client is None:
                raise RuntimeError("no redis client")
            password_rate_limiter = PasswordResetRateLimiter(
                redis_client,
                ip_per_hour=settings.password_reset_ip_per_hour,
                email_per_hour=settings.password_reset_email_per_hour,
                email_salt=settings.password_reset_ip_hash_salt.value(),
            )
        except Exception:  # noqa: BLE001
            # The limiter is fail-open by design; failing to construct it
            # at all is the same posture, so it must not stop the service
            # from starting. The router treats None as "no limit" and
            # logs it once here rather than on every request.
            logging.getLogger(__name__).warning(
                "auth.password.rate_limiter_unavailable_fail_open"
            )

    return ServiceState(
        jwks_cache=jwks_cache,
        app_pool=app_pool,
        tenant_writer_pool=tenant_writer_pool,
        audit_writer_pool=audit_writer_pool,
        audit_reader_pool=audit_reader_pool,
        audit_writer=AuditWriter(audit_writer_pool),
        audit_verifier=AuditVerifier(audit_reader_pool),
        keycloak=keycloak,
        denylist=build_session_denylist(
            enabled=settings.session_revocation_enabled,
            redis_url=settings.redis_url,
        ),
        email_provider=email_provider,
        password_rate_limiter=password_rate_limiter,
        _redis=redis_client,
    )


async def teardown_state(state: ServiceState) -> None:
    await state.jwks_cache.aclose()
    await state.app_pool.close()
    await state.tenant_writer_pool.close()
    await state.audit_writer_pool.close()
    await state.audit_reader_pool.close()
    await state.keycloak.aclose()
    if state.denylist is not None:
        await state.denylist.aclose()
    if state.email_provider is not None:
        await state.email_provider.aclose()
    if state._redis is not None:
        await state._redis.aclose()
    if state.crypto_pool is not None:
        await state.crypto_pool.close()
