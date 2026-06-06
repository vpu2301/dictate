# Runbook — Auth & Audit (Sprint 02)

Scope: incident-response procedures for the auth-service, libs/audit,
the Keycloak realm, and the audit chain. Each section opens with a
*signal* (what alert or symptom triggers this entry) and ends with the
recovery state you're aiming for.

---

## Keycloak outage

**Signal:** `/healthz` of auth-service returns 200 but `/auth/login`
returns 503; `JwksCacheHitRatioLow` may eventually fire as the JWKS
cache expires.

**Behaviour during outage (steady state):**
- Existing access tokens (≤15 min lifespan) continue to verify against
  the JwksCache. Users mid-session are unaffected.
- New logins fail with HTTP 503 from `/auth/login` (Keycloak's token
  endpoint unreachable). The frontend retries.
- Refresh-token rotation fails the same way. After ~15 min of outage,
  the JWKS cache may expire; from that point every verify also fails.

**Recovery:**
1. Restart the Keycloak container (`docker compose restart keycloak`).
   Confirm `/realms/medical-dictation/.well-known/openid-configuration`
   responds 200.
2. Watch `mdx_jwks_refresh_attempts_total` climb — services will
   re-prime their caches naturally on the first verify.
3. No data action required; the audit chain is unaffected (it doesn't
   touch Keycloak).

---

## MFA reset for a locked-out user

**Signal:** A user calls support saying they've lost their phone /
authenticator. (Sprint 02 ships MFA disabled by feature flag, so this
runbook entry applies when `MDX_REQUIRE_MFA=true`.)

**Steps:**
1. Verify the user's identity by an out-of-band channel. **Do not** do
   this over chat.
2. As a `tenant_admin` for the user's tenant, call:
   ```
   POST /admin/users/{sub}/reset-mfa
   ```
   *(Sprint 16+ endpoint — for sprint 02 perform the equivalent action
    in the Keycloak admin console: clear the user's TOTP credential and
    the `mfa_enrolled_at` attribute.)*
3. Audit row appears under `kind=user.reset_mfa`, severity `sec`.
4. Tell the user to log in; they'll be prompted to re-enrol TOTP.

---

## Suspected token theft (refresh-replay alert fired)

**Signal:** `RefreshReplayDetected` alert. The audit log contains a
`kind=auth.refresh_replay_detected` event under the affected tenant.

**Steps:**
1. The auth-service has already attempted to force-revoke the user's
   sessions on detection. Verify in the audit log:
   - Look for `kind=auth.refresh_replay_detected` (tenant_id of the user).
   - The payload's `actor_sub` field carries the user's UUID (extracted
     from the replayed token's unverified claims — adequate for forensics
     since the row + the Postgres log + Keycloak sessions all
     corroborate).
2. As `tenant_admin`, deactivate the user pending investigation:
   ```
   POST /admin/users/{sub}/deactivate
   ```
3. Pull the Keycloak event log for the affected user:
   ```
   GET /admin/realms/medical-dictation/events?user={sub}
   ```
   Look for unusual IPs, user agents, or geolocations.
4. If the user can be reached, ask them to confirm whether they've
   recently used the application from a new device / network. A
   client-side bug that re-sends an old refresh after rotation
   sometimes triggers this alert legitimately.
5. Post-incident: write the timeline into a sec-ops doc; preserve the
   Postgres log for 30 days.

---

## Audit chain divergence (`AuditChainBroken` alert)

**Signal:** `min(mdx_audit_chain_ok) == 0`, or the nightly verifier
exits non-zero. **This is a critical security event.**

**Steps:**
1. **Do not** attempt to "fix" the chain. The divergence row is
   forensic evidence.
2. Identify the affected tenant from the Prometheus gauge:
   ```promql
   mdx_audit_chain_ok == 0
   ```
3. Run `make nightly-verify` manually to confirm and get the exact
   divergence seq:
   ```
   /admin/audit/verify?from_seq=1     (via the auth-service API)
   ```
4. The verifier returns `first_divergence_seq` + `divergence_reason`
   (one of `gap`, `prev_hash_mismatch`, `payload_hash_mismatch`).
5. Check the Postgres logs around the time of the divergence:
   - `SELECT … FROM pg_stat_activity` — who held what connection
   - `pg_audit` extension logs if enabled
   - container-level docker logs for the postgres image
6. If the attacker disabled the immutability trigger to perform the
   edit, the Postgres logs show the `ALTER TABLE … DISABLE TRIGGER`
   statement.
7. Page the security lead. Preserve all logs before any recovery
   action. The chain itself is irreparable — the divergence is
   permanent and that's by design.

---

## JWKS rotation (planned)

**Signal:** Keycloak realm admin rotated the realm signing key.

**Behaviour:**
- libs/auth's JwksCache caches by `kid`. A new token with a new `kid`
  triggers a miss → refresh → cache update. The rate-limit prevents a
  storm of refreshes from forged-`kid` attacks.
- Old tokens (signed by the previous key, still in the JWKS as long as
  Keycloak retains it) continue to verify.

**Action:** none required for routine rotation. If the rotation is
forced because the old key is suspected compromised, also revoke all
active sessions for the realm: `kc.sh import --override` resets keys
and sessions on next start.

---

## Brute-force from a single IP

**Signal:** `LoginFailureBurst` alert. Per-IP source visible in
auth-service logs (`username_hash` is hashed; IP comes from the
reverse-proxy header).

**Steps:**
1. Confirm Keycloak's per-account lockout has kicked in (5 fails / 60s).
   The attacker is now rate-limited per username they're trying.
2. If the attempt is concentrated on one IP, escalate to the network
   team for an upstream block (load balancer / WAF rule).
3. If multiple IPs (distributed attack), increase Keycloak's
   `quickLoginCheckMilliSeconds` (already 1000 ms in dev) and consider
   enabling Keycloak's CAPTCHA flow.
4. Don't rotate signing keys — this is a guessing attack, not a key
   compromise.

---

## auth-service crash / restart

**Signal:** auth-service `/healthz` returns 5xx, `/readyz` fails, or
the container is in CrashLoopBackOff.

**Recovery:**
1. `docker compose logs auth-service --tail 200` for the last
   exception. Common: DB pool exhausted (visible as asyncpg connect
   timeouts), Keycloak unreachable (HTTPx connect errors).
2. Restart the service. The lifespan re-builds all pools + the JWKS
   cache. Cold start is ~3 s.
3. No data action — service is stateless.

---

## Cookie audit (compliance check)

Set-Cookie attributes for `mdx_rt` must be:

```
HttpOnly; Secure; SameSite=Strict; Path=/auth
```

In dev (`AUTH_COOKIE_SECURE=false`) the Secure flag is intentionally
off so cookies work over http://localhost. Staging and production MUST
set `AUTH_COOKIE_SECURE=true` in their env file. Verify with:

```
curl -i -X POST http://staging.example/auth/login -d '…' | grep -i set-cookie
```

The cookie must have `Secure` in staging/prod or treat as a P0 incident.
