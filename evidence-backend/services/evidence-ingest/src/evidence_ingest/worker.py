"""Queue worker: python -m evidence_ingest.worker

Consumes evidence:ingest, runs the pipeline per job. Permanent failures ack
(the job row already carries the dead state + reason); transient failures
go back through the stream's retry/DLQ machinery — a poisoned file cannot
block the stream (spec D3)."""

from __future__ import annotations

import asyncio
import json
import logging
from uuid import UUID

from observability import bootstrap

from evidence_ingest.config import settings
from evidence_ingest.domain.pipeline import PermanentJobError, run_job
from evidence_ingest.main_deps import build_consumer, build_state, teardown_state

logger = logging.getLogger(__name__)


async def run_worker() -> None:
    state = await build_state()
    try:
        consumer = build_consumer(state)
        async with consumer as stream:
            logger.info(
                "ingest worker consuming",
                extra={"stream": settings.ingest_stream, "group": settings.ingest_group},
            )
            async for message in stream:
                try:
                    payload = json.loads(message.value)
                    job_id = UUID(payload["job_id"])
                    tenant_id = UUID(message.headers["tenant_id"])
                except (KeyError, ValueError, json.JSONDecodeError) as exc:
                    logger.error("ingest.malformed_message", extra={"error": str(exc)})
                    await stream.ack(message)
                    continue
                try:
                    final_state = await run_job(state.pipeline, job_id=job_id, tenant_id=tenant_id)
                    logger.info(
                        "ingest.job_finished",
                        extra={"job_id": str(job_id), "state": final_state},
                    )
                    await stream.ack(message)
                except PermanentJobError as exc:
                    logger.warning(
                        "ingest.job_dead", extra={"job_id": str(job_id), "reason": str(exc)}
                    )
                    await stream.ack(message)
                except Exception as exc:
                    logger.exception("ingest.job_retry", exc_info=exc)
                    await stream.fail(message, error_kind="pipeline_error")
    finally:
        await teardown_state(state)


def main() -> None:
    bootstrap(
        f"{settings.service_name}-worker",
        otlp_endpoint=settings.otel_exporter_otlp_endpoint,
        log_level=settings.log_level,
        deployment_environment=settings.environment,
        package_name="evidence-ingest",
        disable_otel=settings.otel_sdk_disabled,
    )
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
