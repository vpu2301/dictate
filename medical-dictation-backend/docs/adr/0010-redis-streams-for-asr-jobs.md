# ADR-0010 — Queue technology: Redis Streams (jobs) + Kafka (events later)

**Date:** 2026-05-22
**Status:** Accepted
**Deciders:** tech lead, SRE/DevOps lead, backend engineering

---

## Context

Sprint 03's ASR pipeline needs a job queue with these properties:

- At-least-once delivery so worker crashes don't lose jobs.
- ~tens-of-ms enqueue latency so a clinician's "submit" doesn't feel
  laggy.
- Consumer groups so multiple worker replicas share a single queue
  without coordination.
- A DLQ / "poison message" landing zone.

Sprint 14 will additionally need a cross-service **event** bus (audit
mirror, NLP postprocess fan-out, …). That's a different shape: durable
log, partition-ordered, multi-consumer fan-out.

Options:

| Tech            | Job queue? | Event bus? | Latency  | Ops cost              |
| --------------- | ---------- | ---------- | -------- | --------------------- |
| Redis Streams   | ✅         | ❌ (small) | ms       | already running       |
| Kafka           | ❌ (heavy) | ✅         | 10s ms   | one more cluster      |
| RabbitMQ        | ✅         | ❌         | ms       | one more cluster      |
| SQS             | ✅         | ❌         | s        | managed, but external |
| NATS JetStream  | ✅         | ✅         | ms       | one more cluster      |

## Decision

- Sprint 03 (jobs): **Redis Streams** via `libs/messaging.RedisStreamsProducer`/
  `RedisStreamsConsumer`. Consumer-group + `XAUTOCLAIM` reclaim + DLQ
  on retries > 3.
- Sprint 14 (events): **Kafka**. Separate impl behind the same
  `ProducerProtocol`/`ConsumerProtocol`.

Two technologies, one Protocol. Each fits the role it's good at.

## Consequences

- Worker latency: well under 100 ms enqueue → consume in dev compose.
- Reuse of existing Redis cluster: no new operational surface in sprint 03.
- `libs/messaging` Protocols stay stable; the sprint-14 Kafka impl
  drops in without service-level rewrites.
- The DLQ is a sibling stream (`{stream}:dlq`); operators see it via
  the same Redis dashboard.

## Alternatives considered

- **Kafka for everything**: Kafka's "queue" mode (compaction +
  consumer-group reclaim) is awkward compared to Streams' built-in
  semantics, and we'd take on Kafka ops cost in sprint 03 with no
  event-bus need yet.
- **NATS JetStream**: a strong contender. Rejected for sprint 03
  because we already run Redis and adding NATS would mean two
  message-broker clusters. Revisit in sprint 14 vs Kafka.
- **RabbitMQ**: classic, but its acker/redelivery semantics with
  consumer crashes are less ergonomic than Streams' pending-entries
  list. Plus another cluster.

## Trigger conditions for revisiting

- Throughput on Streams > 50 k jobs/sec/tenant (we'd partition).
- Need for replay > 24 h after the event (Streams' MAXLEN bounds this).
- Cross-region replication appears (Kafka MirrorMaker would be the
  obvious answer).
