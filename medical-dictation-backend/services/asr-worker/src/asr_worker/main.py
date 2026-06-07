"""asr-worker entry point.

Runs as a long-lived process (no FastAPI surface). Health is exposed via
the GPU compose's `restart: unless-stopped` policy + the metrics it emits
to OTel — there is no listening HTTP server in the worker.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from observability import bootstrap

from .config import settings
from .main_deps import build_state, teardown_state
from .processor import run_forever

logger = logging.getLogger(__name__)


async def _main() -> None:
    bootstrap(
        settings.service_name,
        otlp_endpoint=settings.otel_exporter_otlp_endpoint,
        log_level=settings.log_level,
        deployment_environment=settings.environment,
        package_name="asr-worker",
        disable_otel=settings.testing or settings.otel_sdk_disabled,
    )
    state = await build_state()
    logger.info(
        "asr-worker.started",
        extra={
            "device": settings.asr_device,
            "model": settings.asr_model,
            "stream": settings.asr_jobs_stream,
            "consumer": settings.worker_consumer_name,
        },
    )

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # pragma: no cover  — Windows dev
            loop.add_signal_handler(sig, stop.set)

    runner_task = asyncio.create_task(run_forever(state))
    stop_task = asyncio.create_task(stop.wait())

    done, _pending = await asyncio.wait(
        {runner_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
    )
    for t in done:
        exc = t.exception()
        if exc is not None and not isinstance(exc, asyncio.CancelledError):
            logger.error("asr-worker.task_error", exc_info=exc)

    runner_task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await runner_task

    await teardown_state(state)
    logger.info("asr-worker.stopped")


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":  # pragma: no cover
    main()
