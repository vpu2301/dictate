# ADR-0029 — Redis Streams as the notification event bus

**Status:** Accepted (Sprint 12)
**Supersedes / relates to:** extends ADR-0010 (Redis Streams for ASR jobs)

## Context

Sprints 08–09 produce facts users need to hear about: a report is
finalized, a signing session completes asynchronously via a provider
callback, the chain reconciler finds an integrity failure. None of it
surfaces today.

The naive wiring is a direct HTTP call from each producer to
notification-service at each transition.

## Decision

Producers publish a `NotificationEvent` envelope onto a Redis Stream
(`mdx:notifications:events`); notification-service consumes it with a
consumer group.

## Rationale

A direct HTTP call couples the *clinical* action to the *notification*:

- **Latency.** Finalizing a report would block on notification fan-out,
  which resolves recipients and writes N rows.
- **Availability.** A notification-service deploy or outage would make
  report finalization fail or need a swallowed-exception path in every
  producer — and a swallowed exception is a lost notification with no
  record.
- **Recovery.** An HTTP 500 is gone. A stream entry stays in the
  pending-entries list and is reclaimed.

Streams give at-least-once delivery, horizontal scale via consumer
groups, and a replayable log. This is ADR-0010's reasoning applied to a
second stream, and reuses `libs/messaging` unchanged.

Kafka would also work and is already in the compose stack, but it is not
yet used by any service; introducing a second broker dependency for this
one feature is not justified while Redis is already on the hot path.

## Consequences

- Notification creation is **eventually** consistent with the domain
  action. A user may finalize a report and see the badge a moment later.
  Accepted: the feed is not a transactional read model.
- Producers must be idempotent-friendly — they reuse `event_id` on
  retry, and the consumer derives each row's `dedupe_key` from it.
- Publishing **never raises** (`publish_event` swallows and logs). A
  notification is strictly less important than the action that caused
  it. The cost is that a Redis outage silently drops events; the
  `NotificationStreamLagHigh` and `NotificationConsumerStalled` alerts
  cover the consumer side, and producer-side drops are logged.
- A malformed envelope is dead-lettered on **first** sight rather than
  retried: it can never succeed, and retrying keeps a poison entry
  circulating.
