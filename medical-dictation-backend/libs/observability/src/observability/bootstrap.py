"""Single entry point that wires logs + traces + metrics consistently.

Services call ``bootstrap(...)`` once at startup. It is idempotent: calling
it twice with the same ``service_name`` is a no-op (handlers are not
duplicated). Disabling the OTel SDK via ``disable_otel=True`` is supported
for unit tests.
"""

from __future__ import annotations

from .logging import setup_logging
from .metrics import setup_metrics
from .tracing import setup_tracing


def bootstrap(
    service_name: str,
    *,
    otlp_endpoint: str = "http://localhost:4317",
    log_level: str = "INFO",
    deployment_environment: str = "development",
    package_name: str | None = None,
    disable_otel: bool = False,
    prometheus_port: int | None = None,
) -> None:
    """Configure logs / traces / metrics for a service.

    Args:
        service_name: short, kebab-case name (e.g. ``encounters-service``).
        otlp_endpoint: OTel collector URL.
        log_level: stdlib level for the root logger.
        deployment_environment: ``development`` / ``staging`` / ``production``.
        package_name: distribution name for ``importlib.metadata.version``.
        disable_otel: skip tracing + metrics initialisation (for tests).
        prometheus_port: when set, start a ``/metrics`` HTTP exporter.
    """
    setup_logging(service_name, log_level)
    if disable_otel:
        return
    setup_tracing(
        service_name,
        otlp_endpoint,
        deployment_environment=deployment_environment,
        package_name=package_name,
    )
    setup_metrics(
        service_name,
        otlp_endpoint,
        prometheus_port=prometheus_port,
    )
