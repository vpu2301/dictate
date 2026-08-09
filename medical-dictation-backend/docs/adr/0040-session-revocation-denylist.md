# ADR-0040: Session revocation — auth-service-pushed Redis denylist, fail-open checks

Date: 2026-08-08
Status: Accepted
Sprint: 16

## Context

Threat model: "access tokens stay valid 15 min after logout — accepted
risk; sprint 16 adds a session-revocation listener". The candidate feed
designs:

1. **Keycloak admin-events webhook** — requires deploying a Keycloak
   SPI extension (there is no built-in outbound webhook for logout
   events in our Keycloak 24). Infrastructure work this sprint
   explicitly does not do.
2. **Polling `/admin/events`** — adds constant load and up to a poll
   interval of latency; the events API is not designed as a queue
   (no cursor guarantees across restarts).
3. **Push from auth-service** — every session-ending act in this
   platform already executes inside auth-service (logout, refresh-replay
   force-logout, deactivation, MFA reset), and Keycloak is not publicly
   exposed, so no session ends anywhere else.

## Decision

Option 3. `libs/auth.revocation`:

- **Keys**: `mdx:revoked:sid:{sid}` (one session — logout) and
  `mdx:revoked:sub:{sub}` (every session — deactivation, refresh
  replay). TTL = remaining token lifetime (sid) or
  `MDX_REVOKED_SUB_TTL_SECONDS` (sub, default 1200 s ≥ token lifetime);
  never longer — the set is self-cleaning.
- **Check**: `build_current_user(denylist=…)` — one pipelined Redis
  round-trip after signature verification; wired into every service's
  `current_user` behind `MDX_SESSION_REVOCATION_ENABLED` (same env name
  fleet-wide, default off; off ⇒ bit-for-bit pre-sprint-16 behaviour).
- **Fail-OPEN** on Redis outage, with a WARNING (recorded tradeoff):
  availability over the residual 15-minute window, matching the house
  fail-open pattern for rate limiters. Degraded mode equals the
  pre-sprint-16 posture — never worse.
- **Audit**: `auth.session.revoked` (info on logout, sec on replay/
  deactivation). Reactivation clears the sub-level deny.

## Consequences

- Revocation completeness depends on the "all session ends flow through
  auth-service" invariant. If a future path terminates sessions
  elsewhere, it must push to the denylist or the invariant note in the
  threat model must change.
- A Keycloak-side admin manually logging a user out via the admin
  console does NOT hit the denylist (console use is an operator action;
  runbook says to use the platform API instead).

## Revisit when

- An SPI/webhook lands in the deployment stack → feed the same denylist
  from Keycloak events and remove the invariant dependency.
