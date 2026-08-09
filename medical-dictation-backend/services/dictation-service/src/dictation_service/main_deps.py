"""Service-wide singletons for dictation-service."""

from __future__ import annotations

from dataclasses import dataclass

import asyncpg
import redis.asyncio as aioredis

from asr_worker.inference import WhisperEngine
from audit import AuditWriter
from auth import JwksCache
from crypto import Envelope, TenantKekRepository, build_master_key_provider
from db import create_pool
from storage import EncryptedObjectStore, S3Client

from .config import settings
from .diarization.engine import DiarizationEngine
from .inference import InferenceQueue
from .integrations.nlp_client import NlpClient, NlpClientConfig
from .integrations.report_client import ReportClient, ReportClientConfig
from .integrations.template_client import TemplateClient, TemplateClientConfig
from .session.manager import SessionManager


@dataclass
class ServiceState:
    jwks_cache: JwksCache
    app_pool: asyncpg.Pool
    audit_writer_pool: asyncpg.Pool
    crypto_pool: asyncpg.Pool
    audit_writer: AuditWriter
    redis: aioredis.Redis
    s3: S3Client
    audio_store: EncryptedObjectStore
    envelope: Envelope
    engine: WhisperEngine
    inference_queue: InferenceQueue
    session_manager: SessionManager
    # Sprint 14: conversation mode. The diarization engine is warmed at
    # startup when MDX_DIAR_WARM_AT_STARTUP (the default) — readiness gates
    # conversation capacity on it. Dictation-only deployments set
    # MDX_CONVERSATION_ENABLED=false and never touch torch.
    diarization_engine: DiarizationEngine
    nlp_client: NlpClient
    report_client: ReportClient
    # Sprint-06 client, actually wired in sprint 14 (was dead code: no
    # instance and no bearer existed before the upgrade began retaining
    # the clinician's token).
    template_client: TemplateClient


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

    redis_client = aioredis.from_url(settings.redis_url, decode_responses=False)

    s3 = S3Client(
        endpoint_url=settings.s3_endpoint,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
        region=settings.s3_region,
        use_ssl=settings.s3_use_ssl,
    )
    audio_store = EncryptedObjectStore(
        s3=s3,
        bucket=settings.s3_audio_bucket,
        envelope=envelope,
        disabled=settings.object_store_disabled,
    )

    engine = WhisperEngine()
    if not settings.warm_in_background:
        # Pre-sprint-16 behaviour (dev default): block startup on the load.
        engine.load()

    inference_queue = InferenceQueue(
        transcribe_window_fn=engine.transcribe_window,
        deadline_multiplier=settings.window_inference_deadline_multiplier,
        worker_id=settings.worker_id,
    )

    session_manager = SessionManager(max_sessions=settings.per_worker_max_sessions)

    diarization_engine = DiarizationEngine(
        model_dir=settings.diar_model_dir,
        device=settings.diar_device,
        enabled=settings.conversation_enabled,
        pins={
            "embedding_model.ckpt": settings.diar_model_sha256,
            "mean_var_norm_emb.ckpt": settings.diar_meanvar_sha256,
        },
        model_repo=settings.diar_model_repo,
        model_revision=settings.diar_model_revision,
    )
    # BOTH models warm before the worker is ready. Whisper is warmed
    # eagerly above (engine.load()); the diarizer used to load lazily on
    # the first conversation session, which meant that session paid weight
    # loading inside its first window. Warmup failure is non-fatal — the
    # worker still serves dictation — but /readyz then advertises no
    # conversation capacity (sprint-14 deployment).
    #
    # Sprint 16 (MDX_WARM_IN_BACKGROUND): with large-v3 the combined
    # warmup can exceed the 60 s a probing orchestrator tolerates — the
    # sprint-03 retro's cold-start finding. The background path moves BOTH
    # loads to a lifespan task so /healthz answers immediately while
    # /readyz stays 503 until `engine.is_loaded` — the LB sends no traffic
    # before ready, and liveness never kills a merely-cold pod.
    if settings.diar_warm_at_startup and not settings.warm_in_background:
        await diarization_engine.warm_up()
    nlp_client = NlpClient(
        config=NlpClientConfig(
            base_url=settings.nlp_base_url,
            timeout_seconds=settings.finalize_nlp_timeout_seconds,
        )
    )
    report_client = ReportClient(
        config=ReportClientConfig(
            base_url=settings.report_base_url,
            timeout_seconds=settings.report_draft_timeout_seconds,
        )
    )
    template_client = TemplateClient(
        config=TemplateClientConfig(base_url=settings.report_base_url)
    )

    return ServiceState(
        jwks_cache=jwks_cache,
        app_pool=app_pool,
        audit_writer_pool=audit_writer_pool,
        crypto_pool=crypto_pool,
        audit_writer=AuditWriter(audit_writer_pool),
        redis=redis_client,
        s3=s3,
        audio_store=audio_store,
        envelope=envelope,
        engine=engine,
        inference_queue=inference_queue,
        session_manager=session_manager,
        diarization_engine=diarization_engine,
        nlp_client=nlp_client,
        report_client=report_client,
        template_client=template_client,
    )


async def teardown_state(state: ServiceState) -> None:
    await state.jwks_cache.aclose()
    await state.redis.aclose()
    await state.app_pool.close()
    await state.audit_writer_pool.close()
    await state.crypto_pool.close()
    await state.s3.aclose()
    await state.nlp_client.aclose()
    await state.report_client.aclose()
    await state.template_client.aclose()
