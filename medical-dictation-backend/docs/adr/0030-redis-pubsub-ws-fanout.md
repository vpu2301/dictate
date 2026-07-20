# ADR-0030 — Redis pub/sub for cross-worker WebSocket fan-out

**Status:** Accepted (Sprint 12)

## Context

`/ws/notifications` sockets are pinned to whichever worker accepted the
upgrade. A notification materialised by the ingest consumer on worker A
must reach a user whose socket lives on worker B.

## Decision

Every worker subscribes to the pattern `mdx:notify:user:*`. Fan-out
publishes a frame keyed by **recipient**; each worker forwards to the
sockets it holds locally and ignores the rest.

## Rationale

Alternatives considered:

- **A shared socket registry** (Redis hash of user → worker). Requires
  cleanup after every crashed worker; a stale entry means publishing to
  a dead worker forever. The failure mode is silent.
- **Sticky routing at the load balancer.** Pushes the problem into infra
  and breaks whenever a worker is replaced.
- **Polling.** Defeats the point of a socket.

Pub/sub is stateless: a worker that holds no socket for a user does
nothing, and a dead worker simply stops receiving. There is nothing to
reconcile.

## Consequences

- Pub/sub is **fire-and-forget and unordered**. It is an optimisation,
  never the source of truth: the unread count always comes from the
  database, and a reconnecting client re-reads it (mitigates E5).
- Published frames carry **ids only**. The receiving worker re-reads the
  row under the recipient's tenant scope before sending, so pub/sub —
  which has no tenant isolation of its own — never carries notification
  content and cannot become a cross-tenant leak.
- The channel is keyed by recipient, not by notification: a subscriber
  can only match against the users it holds sockets for.
- Fan-out is best-effort per socket; a send failure drops that socket
  and the client recovers on reconnect.
