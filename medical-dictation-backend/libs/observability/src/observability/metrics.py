"""OTel metrics setup.

Exports metrics two ways:

* OTLP gRPC to the collector (network metrics, downstream Prometheus).
* Prometheus pull endpoint exposed at ``/metrics`` on a sidecar port for
  services that prefer scrape over push.

Default histogram buckets follow request-latency expectations stated in the
sprint spec: ``5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000`` ms.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from opentelemetry import metrics
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import Resource

logger = logging.getLogger(__name__)

LATENCY_BUCKETS_MS: Sequence[float] = (
    5.0,
    10.0,
    25.0,
    50.0,
    100.0,
    250.0,
    500.0,
    1000.0,
    2500.0,
    5000.0,
    10000.0,
)


def setup_metrics(
    service_name: str,
    otlp_endpoint: str = "http://localhost:4317",
    export_interval_ms: int = 30_000,
    *,
    prometheus_port: int | None = None,
) -> None:
    """Initialise OTel metrics. Optionally start a Prometheus pull endpoint."""
    resource = Resource.create({"service.name": service_name})
    otlp_reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=otlp_endpoint, insecure=True),
        export_interval_millis=export_interval_ms,
    )
    readers: list[MetricReader] = [otlp_reader]

    if prometheus_port is not None:
        try:
            from opentelemetry.exporter.prometheus import PrometheusMetricReader
            from prometheus_client import start_http_server

            start_http_server(prometheus_port)
            readers.append(PrometheusMetricReader())
        except Exception as exc:  # pragma: no cover
            logger.warning("Prometheus exporter not started: %s", exc)

    latency_view = View(
        instrument_name="*latency*",
        aggregation=ExplicitBucketHistogramAggregation(boundaries=LATENCY_BUCKETS_MS),
    )

    provider = MeterProvider(resource=resource, metric_readers=readers, views=[latency_view])
    metrics.set_meter_provider(provider)
