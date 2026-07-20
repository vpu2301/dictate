# Runbook — notification-service

Sprint 12. Every alert in `infra/prometheus/rules/sprint-12-alerts.yml`
links to an anchor in this file.

## Shape of the system

```
producers (see the table below)
   → XADD mdx:notifications:events
   → notification-service ingest consumer group `notification-workers`
   → notifications (RLS) + notification_outbox (one row per channel)
   → in-app: WS fan-out via Redis pub/sub  |  email: delivery worker → SMTP
```

### Who publishes what

The first question in any "I did X and got nothing" report is whether
anything publishes for X at all. It is not always yes.

| category | published by | file |
| --- | --- | --- |
| `report.finalized` | report-service | `routers/reports_lifecycle.py` |
| `report.signed` / `report.amended` | report-service | `routers/reports_sign.py` |
| `report.chain_failure` | report-service | `jobs/chain_reconciler.py` |
| `dictation.completed` | dictation-service | `ws/handler.py::_finalize_normal` |
| `transcription.completed` | asr-worker | `processor.py::_process_one` |
| `transcription.failed` | asr-worker | `processor.py::_mark_failed` |
| `system.digest` | notification-service | `jobs/digest.py` (not via the bus) |

**No producer exists** for `report.signing_failed` or
`report.shared_with_you` — they have catalog entries, renderers and
email templates, but nothing emits them. Nor is anything emitted for a
cancelled ASR job, or for creating a report from a transcript
(`POST /v1/reports/from-transcript`) — that one is a synchronous action
whose 201 is the user's confirmation.

Every producer is gated by `MDX_NOTIFICATIONS_ENABLED` and needs
`REDIS_URL`; a producer whose Redis is unset publishes into the void
**silently**, because `publish_event` swallows its own failures by
design (ADR-0029).

Key invariants:

- Materialisation is idempotent on `notifications.dedupe_key`
  (`{event_id}:{recipient}`).
- Delivery is idempotent on `notification_outbox (notification_id, channel)`.
- A channel that was deliberately not sent has a `suppressed` row with a
  `suppressed_reason` — absence of a row means a bug, not a suppression.

## Quick triage

```bash
# Is the consumer keeping up?
redis-cli XINFO GROUPS mdx:notifications:events

# What is stuck?
psql -c "SELECT status, count(*) FROM notification_outbox GROUP BY status;"

# What was abandoned?
psql -c "SELECT created_at, source, channel, last_error
           FROM audit.notification_dead_letters
          ORDER BY created_at DESC LIMIT 20;"
```

---

## <a id="alert-stream-lag"></a>NotificationStreamLagHigh

Backlog > 500 for 10m.

This alert matters more than it looks: **if the consumer is dead, no
other notification alert can fire**, because nothing is attempted. A
silent system looks identical to a healthy one.

1. `redis-cli XINFO GROUPS mdx:notifications:events` — check `pending`
   and `consumers`.
2. If `consumers` is 0, no replica is attached: check
   notification-service is running and `MDX_NOTIFICATION_INGEST` is not
   `false`.
3. If consumers exist but `pending` grows, look for a poison message:
   `docker logs` for `ingest.transient_failure` repeating on one id.
   A genuinely undeliverable envelope should be dead-lettered on first
   sight; a *transient* error that never resolves (DB down) will loop.
4. Scale: add replicas. **Each replica needs a unique
   `MDX_NOTIFICATION_CONSUMER_NAME`** — two replicas sharing a name
   share a pending-entries list, and a crash on one hands the other's
   in-flight work over.

## <a id="alert-consumer-stalled"></a>NotificationConsumerStalled

Events pending, none consumed, for 15m. Same procedure as above; this
one distinguishes "nothing to do" from "cannot do it".

## <a id="alert-delivery-failures"></a>NotificationDeliveryFailureRateHigh

\>25% of attempts failing.

Almost always the email provider (E7). In-app is unaffected — the system
degrades rather than fails.

1. `SELECT last_error, count(*) FROM notification_outbox
    WHERE status IN ('pending','dead') AND last_error <> ''
    GROUP BY 1 ORDER BY 2 DESC LIMIT 5;`
2. Check the relay directly. In dev the sink is Mailpit at
   <http://localhost:8025>.
3. Rows retry automatically with exponential backoff (30s base, capped
   at 1h). **Do not** manually re-drive unless the provider is confirmed
   healthy — you will just burn attempts toward the dead-letter cap.
4. To re-drive after a confirmed fix:
   ```sql
   UPDATE notification_outbox
      SET next_attempt_at = now()
    WHERE status = 'pending' AND next_attempt_at > now();
   ```

## <a id="alert-dead-letter"></a>NotificationDeadLetterPresent

Fires on **one** dead-letter in an hour. Deliberately sensitive: unlike a
retry, a dead-letter is terminal and silent — someone will never be told
something.

1. Read `audit.notification_dead_letters` (see Quick triage).
2. `source='ingest'` → a producer emitted a malformed envelope. The
   `envelope` column holds it verbatim. Fix the producer; the event is
   lost and must be re-emitted if it mattered.
3. `source='delivery'` → the address or the provider. Check
   `last_error`.
4. Re-driving a dead row is a **deliberate** act:
   ```sql
   UPDATE notification_outbox
      SET status='pending', attempt_count=0, next_attempt_at=now()
    WHERE id = '<outbox_id>';
   ```

## <a id="alert-digest-stale"></a>NotificationDigestStale

No successful digest run in 26h (not 24h — a merely late run should not
page).

1. Run it manually: `make run-notification-digest`.
2. Check `notification_digest_progress` for rows with `claimed_at` set
   and `finished_at` NULL — that is a **crashed run**, and it is
   deliberately not auto-retried: the claim was taken before the send,
   so a blind retry risks a second email (E6).
3. To genuinely re-run a user-day after confirming no mail went out,
   delete the claim row for that `(digest_date, tenant_id, user_id)`.
   **Never bulk-delete claims** — that is how everyone gets a duplicate
   digest.

## <a id="alert-storm"></a>NotificationCoalescingSustained

Coalescing is *working* when this fires (E1) — but sustained coalescing
means a producer is flooding the bus.

1. `SELECT category, count(*) FROM notifications
    WHERE created_at > now() - interval '1 hour'
    GROUP BY 1 ORDER BY 2 DESC;`
2. The usual culprit is the chain reconciler flagging a large backlog.
   It emits one event per *report*, not per anomaly — if you see one per
   anomaly, that regression is back.
3. Emergency stop at the source: set `MDX_NOTIFICATIONS_ENABLED=false`
   on the offending producer and restart it. In-app history is
   unaffected; only new events stop.
4. Tune with `MDX_NOTIFICATION_RATE_CAP` / `MDX_NOTIFICATION_RATE_WINDOW_S`.

## <a id="alert-fanout-latency"></a>NotificationFanoutLatencyHigh

WS fan-out p95 > 1s. Live badges lag; **nothing is lost** — the DB is the
source of truth and clients re-read on reconnect (E5).

1. Check Redis health and `mdx_notification_connected_sockets` per
   worker; a single worker holding every socket is a load-balancer
   stickiness problem.
2. Verify the pub/sub bridge is subscribed: logs show
   `fanout.recv_failed` on a broken subscription.

---

## Common operations

**Turn off a category for a user** — they can do it themselves at
`PUT /v1/notifications/preferences`. Suppressed channels still write an
outbox row with `suppressed_reason='preference'`, which is the auditable
proof if they later say they got one anyway.

**Verify the PHI boundary after a template change**

```bash
make check-notification-phi-free
```

Blocking in `make ci`. Any change to a template or to
`ALLOWED_PAYLOAD_KEYS` needs DPO sign-off (ADR-0031).

**Replay an event by hand** (dev)

```bash
redis-cli XADD mdx:notifications:events '*' \
  value '{"event_id":"…","tenant_id":"…","category":"report.finalized", …}'
```

Reusing an `event_id` is safe: `dedupe_key` collapses it.

**"I did X and got no notification"** — the usual cause is that the
event resolved to zero recipients, which is logged at DEBUG and is
otherwise silent. Confirm before looking anywhere else:

```bash
# Did the producer publish at all?
redis-cli XREVRANGE mdx:notifications:events + - COUNT 5

# Did the consumer keep up? `lag` should be 0 and `pending` small.
redis-cli XINFO GROUPS mdx:notifications:events

# Did it materialise into rows?
psql -c "SELECT category, recipient_user_id, created_at
           FROM notifications ORDER BY created_at DESC LIMIT 10;"
```

Events present in the stream with `lag 0` but no matching row means
`resolve_recipients` returned empty. Check the category's
`exclude_actor` in `domain/catalog.py` against the event's
`actor_user_id` and `recipient_hints` — if the only hint IS the actor
and the category excludes them, nothing is created. That was the
sprint-12 `report.finalized` defect.

**Adding a category** touches four places that must agree, or the
consumer DLQs every event of the new kind:

1. `libs/notification_events/enums.py` — the `Category` member.
2. `notification_service/domain/catalog.py` — the `CategorySpec`
   (a test asserts 1:1 with the enum, so this one fails loudly).
3. `notification_service/domain/render.py` — the payload allow-list
   plus a `case` arm in `render_title`/`render_body`.
4. **A migration widening the `category` CHECK on `notifications` AND
   `notification_preferences`.** This is the one with no test in front
   of it: skip it and every event of the new category raises
   `CheckViolationError` inside the consumer and lands in the DLQ. See
   `0052_notification_dictation_category.sql` and
   `0053_notification_transcription_categories.sql`.

Then `make openapi-dump`, and mirror the enum in the SPA's
`src/notifications/constants.js`.
