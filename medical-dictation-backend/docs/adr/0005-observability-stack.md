# ADR-0005 — Observability Stack

**Date:** 2026-05-09
**Status:** Accepted
**Deciders:** Backend tech lead, SRE/DevOps

---

## Context

The platform handles PHI under HIPAA-equivalent obligations (Ukrainian
Закон «Про захист персональних даних» plus contractual EU-GDPR
alignment). We need traces for latency debugging, metrics for SLOs and
capacity, and logs for forensics — all three correlatable by trace ID,
all three filtered for PII before any byte leaves the process.

## Decision

* **Logs** — stdlib `logging` → JSON via `python-json-logger` →
  stdout → Promtail → **Loki**. A `CorrelationIdFilter` injects OTel
  `trace_id` and `span_id` into every record. A `PIISafeFilter` runs last;
  values whose key is in the drop list are removed, mask-list keys are
  replaced with `<redacted>`.
* **Traces** — OTel SDK with OTLP gRPC → OTel Collector → **Tempo**
  (planned) / Jaeger (Sprint 1). Resource attributes set
  `service.name`, `service.namespace=medical-dictation`,
  `service.version`, `deployment.environment`. W3C `tracecontext` and
  baggage propagators install globally. Auto-instrumentation wired for
  FastAPI, asyncpg, httpx.
* **Metrics** — OTel SDK with OTLP gRPC + a Prometheus pull endpoint at
  `/metrics`. Default histogram buckets for latency:
  `[5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000]` ms.
  Naming convention: `mdx_<domain>_<unit_or_action>_<unit>` (snake_case,
  `mdx` prefix, units as suffix per OTel semantic conventions).
* **Single entry point** — services call `observability.bootstrap(...)`
  once at startup. Tests pass `disable_otel=True` to skip the SDK.

## Consequences

**Positive**

- One canonical setup function; new services bootstrap consistently.
- PII filter is deterministic, exact-match-or-known-suffix on field
  names. Easy to audit; no value-content guessing.
- Trace ID in every log line means a forensic investigation can pivot
  cleanly across the three telemetry surfaces.

**Negative**

- The drop list is a moving target. Sprint 2+ will expand it as new
  sensitive fields are introduced. Mitigation: drop list lives in code,
  reviewed in PRs, with a 50+ case test corpus.
- Prometheus vs OTLP duplication for metrics costs a small amount of
  CPU. We accept that for the scrape ergonomics.
- Tempo is the production trace backend (Sprint 16). Sprint 1 ships
  Jaeger for simplicity; the migration is a config change.

## Alternatives considered

- **structlog** — strong choice. The ergonomics of stdlib logging plus a
  JSON formatter are equivalent for our payloads, with one less
  dependency. We may revisit if we need richer event-dict processors.
- **Datadog / New Relic / Honeycomb agents** — vendor lock-in and PHI
  egress problems. OTel + self-hosted Loki/Tempo/Prometheus is the
  default until production economics tell us otherwise.

## Trigger conditions for revisiting

- A vendor offers a meaningfully better PHI-safe ingestion path.
- The PII drop list catches a regression in production (escalates the
  filter to value-content heuristics).
- Cardinality / volume forces a sampling strategy.
