# WebSocket protocol `medical-notifications.v1`

Sprint 12. This document is the **byte-for-byte frontend contract**. The
authoritative source is
`services/notification-service/src/notification_service/ws/protocol.py`;
if the two disagree, the code wins and this file is stale — fix it.

## Endpoint

```
ws(s)://<notification-service>/ws/notifications
```

Default port in compose: `8004` (host) → `8000` (container).

## Handshake

The client **MUST** offer the subprotocol `medical-notifications.v1`.
The subprotocol string *is* the version negotiation: a client that does
not offer it is rejected before `accept()`, because serving it would
mean guessing which frame shape it understands.

```js
const ws = new WebSocket(
  `${base}/ws/notifications?token=${accessToken}`,
  ["medical-notifications.v1"],
);
```

### Authentication

A bearer token, in either:

1. `Authorization: Bearer <jwt>` — preferred, but browsers cannot set
   headers on a WebSocket handshake.
2. `?token=<jwt>` query parameter — the SPA path. Accepted for that
   reason alone; use short-lived access tokens.

Validation happens **before** `accept()`.

### Rejection: what a client actually observes

Checked in order: origin → subprotocol → JWT.

**A rejected upgrade is NOT distinguishable by close code.** Because the
rejection happens before `accept()`, Starlette answers the handshake with
a plain **HTTP 403** — whatever code the endpoint passes — and no
WebSocket close frame is ever sent. A browser therefore reports an
abnormal closure (**`1006`**), *not* `4401`/`4403`.

Verified against the running service by
`src/notifications/contract.int.test.js` in the SPA repo: a handshake
missing the subprotocol and one missing the token both return `403`.

**Client implication:** do not build token-refresh logic on seeing
`4401` — it will never arrive on this path, and an expired token would
never self-heal. Treat an abnormal close as possibly-auth and refresh
before reconnecting (`socket.js:classifyClose` does this).

The codes below are the endpoint's *intent*, reachable only if the
server is changed to accept-then-close. They are documented so the
mapping is unambiguous if that change is made.

| Close code | Intent | Observable today? |
| ---------- | ------ | ----------------- |
| `4400` | Subprotocol not offered / malformed handshake | No — HTTP 403 |
| `4401` | Missing, expired or invalid token | No — HTTP 403 |
| `4403` | Origin not in the allow-list | No — HTTP 403 |
| `4429` | Rate limited | Not emitted: the notification upgrade has no rate limiter (dictation's does) |
| `1008` | Any other policy violation | No |

> **Known gap.** Conveying a reason to a browser requires
> accept-then-close. Until that lands, a client cannot tell "your token
> expired" from "the server is down". Tracked for the sprint-12
> sign-off.

## Frames

Text frames only. JSON. A discriminated union on `type`. **Every model
is `extra="forbid"`** — an unknown field is a contract violation, not
something to ignore.

### Server → client

#### `connected`
Sent immediately after `accept()`.

```json
{ "type": "connected",
  "subprotocol": "medical-notifications.v1",
  "unread_count": 3 }
```

#### `notification`
A newly materialised notification for this user.

```json
{ "type": "notification",
  "notification": {
    "id": "0f9c…",
    "category": "report.signed",
    "title": "Звіт RPT-2026-0042 підписано",
    "body_text": "Кваліфікований електронний підпис … накладено.",
    "deep_link": "https://app.example/reports/8a1f…",
    "resource_type": "report",
    "resource_id": "8a1f…",
    "severity": "info",
    "created_at": "2026-07-19T09:14:03.221Z",
    "read_at": null
  },
  "unread_count": 4 }
```

`notification` is **field-identical to a REST feed item**, so one
rendering code path serves both a pushed frame and a fetched page.

`category` is a closed vocabulary — the enum in
`libs/notification_events/enums.py`, mirrored in the OpenAPI snapshot:

| category | resource_type | addressed to |
| --- | --- | --- |
| `report.finalized` | `report` | author + co-authors, **including the actor** |
| `report.signed` | `report` | author + co-authors, including the signer |
| `report.signing_failed` | `report` | author + co-authors, including the actor |
| `report.amended` | `report` | author + co-authors, minus the actor |
| `report.chain_failure` | `report` | tenant admins |
| `report.shared_with_you` | `report` | the named recipients |
| `dictation.completed` | `dictation_session` | the dictating clinician only |
| `transcription.completed` | `transcription_job` | the submitting user only |
| `transcription.failed` | `transcription_job` | the submitting user only (severity `warning`) |
| `system.digest` | — | one named user |

A client MUST ignore a category it does not recognise rather than
failing the frame: the vocabulary grows additively within v1, and
`dictation.completed` and the `transcription.*` pair were both added
after the first clients shipped.

Note the "including the actor" rows. Categories that are a *receipt* for
something you did yourself deliberately do not exclude you — excluding
the actor from `report.finalized` meant a solo clinician finalizing
their own report generated no notification at all.

#### `unread_count`
The badge changed without a specific new notification — e.g. the user
marked something read in another tab.

```json
{ "type": "unread_count", "unread_count": 2 }
```

#### `read_ack`
Response to `mark_read`.

```json
{ "type": "read_ack", "notification_id": "0f9c…", "unread_count": 2 }
```

#### `pong`
Response to `ping`.

```json
{ "type": "pong" }
```

#### `error`
A client frame failed validation. The socket **stays open** — a client
bug should be visible to the client, not look like a network fault.

```json
{ "type": "error", "code": "bad_frame", "detail": "frame failed validation" }
```

### Client → server

#### `mark_read`
```json
{ "type": "mark_read", "notification_id": "0f9c…" }
```
Idempotent: marking an already-read notification succeeds and does not
move `read_at`. A user cannot mark someone else's notification read even
by guessing the id — the update is filtered on `recipient_user_id` under
RLS.

#### `ping`
```json
{ "type": "ping" }
```
There is no server-initiated heartbeat; clients that need keepalive
through an idle proxy should ping.

## Client obligations

1. **The socket is not the source of truth.** Redis pub/sub fan-out is
   fire-and-forget (ADR-0030). On connect *and* on every reconnect,
   trust the `unread_count` in `connected`, or re-fetch
   `GET /v1/notifications/unread-count`.
2. **Reconnect with backoff.** Do not reconnect in a tight loop on a
   `4401`; refresh the token first.
3. **Tolerate duplicates.** Delivery is at-least-once end to end. Frames
   carry a stable `id`; de-duplicate on it.
4. **Do not parse `deep_link`.** Treat it as opaque and navigate to it.

## Versioning

Adding an optional field to an existing frame is **not** breaking.
Removing a field, renaming one, or changing a discriminator value **is**
— those require `medical-notifications.v2` as a new subprotocol string,
run alongside v1 for a deprecation window.
