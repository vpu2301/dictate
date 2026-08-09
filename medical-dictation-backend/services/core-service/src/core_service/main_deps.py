"""Service-wide singletons for core-service."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import asyncpg

from audit import AuditWriter
from auth import JwksCache
from crypto import Envelope, TenantKekRepository, build_master_key_provider
from db import create_pool
from storage import EncryptedObjectStore, S3Client

from .config import settings

logger = logging.getLogger(__name__)


@dataclass
class ServiceState:
    """Singletons wired at startup; reached by routers via deps.get_state()."""

    jwks_cache: JwksCache
    app_pool: asyncpg.Pool
    audit_writer_pool: asyncpg.Pool
    audit_writer: AuditWriter
    # Envelope crypto for raw-ІПН retention; wired only when
    # PATIENT_IPN_RAW_ENABLED=true (DPO-gated, default off).
    envelope: Envelope | None = None
    crypto_pool: asyncpg.Pool | None = None
    # 0065 — patient record attachments. Envelope-encrypted into MinIO; the
    # table holds only metadata. None when crypto could not be wired, and the
    # upload surface answers 503 rather than storing a file in the clear.
    document_store: EncryptedObjectStore | None = None


async def build_state() -> ServiceState:
    jwks_cache = JwksCache(issuer_to_url={settings.auth_issuer: settings.auth_jwks_url})

    app_pool = await create_pool(
        settings.db_app_role_dsn,
        application_name=f"{settings.service_name}/app",
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
    )
    audit_writer_pool = await create_pool(
        settings.db_audit_writer_dsn,
        application_name=f"{settings.service_name}/audit_writer",
        min_size=1,
        max_size=4,
    )
    audit_writer = AuditWriter(audit_writer_pool)

    envelope: Envelope | None = None
    crypto_pool: asyncpg.Pool | None = None
    document_store: EncryptedObjectStore | None = None
    # Envelope crypto is wired for raw-ІПН retention (DPO-gated) AND for
    # patient attachments, which are always on: a file dropped on a patient's
    # record is PHI and cannot be stored without it. A wiring failure degrades
    # the upload route to 503 rather than taking the service down — unless
    # raw-ІПН retention is on, which cannot run without crypto at all.
    try:
        crypto_pool = await create_pool(
            settings.db_crypto_writer_dsn,
            application_name=f"{settings.service_name}/crypto_writer",
            min_size=1,
            max_size=2,
        )
        master = build_master_key_provider(
            provider=settings.master_key_provider,
            file_path=settings.master_key_path,
            vault_addr=settings.vault_addr,
            vault_token=settings.vault_token,
            vault_transit_key=settings.vault_transit_key,
            vault_transit_mount=settings.vault_transit_mount,
        )
        await master.startup_self_check()
        kek_repo = TenantKekRepository(pool=crypto_pool, master_key_provider=master)
        envelope = Envelope(master_key_provider=master, kek_repository=kek_repo)
        document_store = EncryptedObjectStore(
            s3=S3Client(
                endpoint_url=settings.s3_endpoint,
                access_key=settings.s3_access_key,
                secret_key=settings.s3_secret_key,
                region=settings.s3_region,
                use_ssl=settings.s3_use_ssl,
            ),
            bucket=settings.s3_patient_docs_bucket,
            envelope=envelope,
        )
        logger.info("envelope crypto wired (patient documents, raw-ІПН)")
    except Exception as exc:
        logger.warning(
            "core.envelope_wiring_failed: patient document upload answers 503 "
            "until crypto is reachable (%s)",
            exc,
        )
        if settings.patient_ipn_raw_enabled:
            raise

    return ServiceState(
        jwks_cache=jwks_cache,
        app_pool=app_pool,
        audit_writer_pool=audit_writer_pool,
        audit_writer=audit_writer,
        envelope=envelope,
        crypto_pool=crypto_pool,
        document_store=document_store,
    )


async def teardown_state(state: ServiceState) -> None:
    await state.jwks_cache.aclose()
    await state.app_pool.close()
    await state.audit_writer_pool.close()
    if state.crypto_pool is not None:
        await state.crypto_pool.close()
