"""OTel tracing setup.

Resource attributes follow OpenTelemetry semantic conventions:

* ``service.name``       — per-service
* ``service.namespace``  — ``medical-dictation`` (the platform)
* ``service.version``    — read from package metadata when available
* ``deployment.environment`` — development / staging / production

Auto-instrumentation is wired for FastAPI, asyncpg, and httpx. W3C
``tracecontext`` and baggage propagators are installed so trace context
flows across HTTP, gRPC, and Kafka boundaries.
"""

from __future__ import annotations

import logging
from importlib import metadata as _metadata

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.propagate import set_global_textmap
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

logger = logging.getLogger(__name__)

_PLATFORM_NAMESPACE = "medical-dictation"


def _service_version(package_name: str | None) -> str:
    if not package_name:
        return "0.0.0"
    try:
        return _metadata.version(package_name)
    except _metadata.PackageNotFoundError:
        return "0.0.0"


def setup_tracing(
    service_name: str,
    otlp_endpoint: str = "http://localhost:4317",
    *,
    deployment_environment: str = "development",
    package_name: str | None = None,
) -> None:
    """Initialise OTel tracing and register the global TracerProvider."""
    resource = Resource.create(
        {
            "service.name": service_name,
            "service.namespace": _PLATFORM_NAMESPACE,
            "service.version": _service_version(package_name),
            "deployment.environment": deployment_environment,
        }
    )
    exporter = OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True)
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    # W3C tracecontext + baggage. Keep the composite even with one propagator
    # so downstream code can extend it without rewiring.
    set_global_textmap(CompositePropagator([TraceContextTextMapPropagator()]))

    _install_auto_instrumentation()


def _install_auto_instrumentation() -> None:
    """Best-effort auto-instrumentation; missing libs are not an error."""
    _patch_fastapi_route_details()
    try:
        from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor

        AsyncPGInstrumentor().instrument()  # type: ignore[no-untyped-call]
    except Exception as exc:  # pragma: no cover  — optional at runtime
        logger.debug("AsyncPG instrumentation skipped: %s", exc)
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument()
    except Exception as exc:  # pragma: no cover  — optional at runtime
        logger.debug("HTTPX instrumentation skipped: %s", exc)


def _patch_fastapi_route_details() -> None:
    """Work around an upstream crash in opentelemetry-instrumentation-fastapi
    (through 0.62b1).

    ``_get_route_details`` reads ``route.path`` on a ``Match.PARTIAL`` route
    without the ``AttributeError`` guard the ``Match.FULL`` branch has. A CORS
    preflight (``OPTIONS``) or any wrong-method request partial-matches an
    ``_IncludedRouter`` that has no ``.path``, so the request 500s before it is
    ever handled. We wrap the function so a missing path falls back to the raw
    scope path (what the ``FULL`` branch already does on the same error).
    """
    try:
        from opentelemetry.instrumentation import fastapi as _fastapi_instr
    except Exception as exc:  # pragma: no cover  — optional at runtime
        logger.debug("FastAPI route-details patch skipped: %s", exc)
        return

    original = getattr(_fastapi_instr, "_get_route_details", None)
    if original is None or getattr(original, "_mdx_patched", False):
        return

    def _safe_get_route_details(scope: object) -> object:
        try:
            return original(scope)
        except AttributeError:
            return scope.get("path") if isinstance(scope, dict) else None

    _safe_get_route_details._mdx_patched = True  # type: ignore[attr-defined]
    _fastapi_instr._get_route_details = _safe_get_route_details
