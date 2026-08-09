# Runbook — Signing Service

## Health checks

- `GET /healthz` — process liveness.
- `GET /readyz` — providers + trust anchors.
- Provider health refreshed every 30s by the cron job; the table
  `signing_provider_health` is the dashboard's source of truth.
- Daily privacy test (sprint-07) runs against the HF demo; signing
  service has its own daily synthetic-sign canary in sprint-17.

## Incident playbooks

### Provider outage (Дія or ІІТ)

Symptoms: `signing_provider_health.healthy=false` for ≥ 2 min;
session-init returns `503 no_signing_provider_available` or auto-falls
back to the other provider.

1. Confirm with `curl <provider>/health` from a sandbox VM.
2. If single-provider outage: signing-service fallback kicks in
   automatically (selection logic in
   `medical_kep.selection.select_providers`). Pilot UX shows only the
   healthy provider.
3. If both providers down: surface "Signing temporarily unavailable —
   please retry in 5 minutes" to the FE. Page on-call.
4. Recovery: 30s health monitor re-flips the row to `healthy=true`
   on first successful probe; users see availability immediately.

### Certificate chain validation failure

Symptoms: `signing.session.failed` audit events with
`failure_reason='envelope_verification_failed:untrusted_certificate_chain'`.

1. Run `scripts/admin/inspect_envelope.py <session_id>` (sprint-10
   placeholder; for sprint-09 inspect via SQL) to extract the
   certificate chain.
2. Verify the leaf's issuer cert is present in
   `infra/trust-store/ca-bundle.pem`.
3. If missing, this is a **possible CA revocation event** (the КНЕДП
   stopped operating, lost its qualification, or rotated keys
   unexpectedly). Trigger the trust-store update procedure.

### Callback signature mismatch

Symptoms: `signing.session.callback_signature_invalid` audit events.

Likely causes:
- Provider rotated their callback signing key. Refresh the public-key
  cache (`DiiaProvider._fetch_public_keys` re-fetches on TTL expiry).
- Replay attack. Investigate the audit row's `requestor_ip_hmac`.
- Bug in provider configuration (wrong HMAC key for ІІТ). Audit the
  `iit_callback_hmac_key_hex` env var.

### Stuck session

Symptoms: session in `verifying` for > 5 minutes.

The 60s reaper marks these as `failed` with
`failure_reason='verification_stuck'`. Investigation:

1. Pull the session row + the linked `provider_session_id`.
2. Check provider logs (Дія: dashboard at api.diia.gov.ua; ІІТ: helper
   logs on the user's machine).
3. Manual force-finalize is **forbidden** at sprint-09; the envelope
   either parsed or it didn't. A "stuck" session means the user
   probably abandoned mid-sign — wait for the reaper.

### Trust store rollback

If a malicious / wrong CA cert was merged:

1. `git revert <merge-commit>` on the PR that introduced it.
2. CI rebuilds the signing-service container.
3. Staged rollout: canary first, then 25% / 50% / 100%.
4. **All envelopes signed under the removed CA become invalid going
   forward**. Previously-verified envelopes remain in the DB but their
   re-verification will fail — flagged in audit + escalated.

### Public verify rate limit hit

Symptoms: spike in `audit.public_verify_audit` rows with
`result='rate_limited'`.

Investigate via the `requestor_ip_hmac` distribution:

- One IP burning the limit: legitimate burst (court reviewing many
  reports) or scripted abuse.
- Many IPs: distributed enumeration attempt. Tighten the limiter
  temporarily via `PUBLIC_VERIFY_RATE_PER_MINUTE` and consider WAF
  rules (sprint-16 adds Cloudflare).

### Someone cannot sign / `403 signing_not_permitted`

**Signing is clinician-only.** Applying a qualified electronic signature
to a clinical record is a clinician's personal legal act under Law
2155-VIII, so it is gated on `report.sign` (`report.amend` for amending a
signed report, `consent.sign` for a patient consent) — held by
`clinician` and by no other role.

Before the hotfix every signing surface gated on `report.write`, which
`nurse` holds, and the consent surface on `patient.write`, which `nurse`
and `tenant_admin` both hold. A nurse could sign a clinical report; a
nurse or an operational admin could sign a consent. If you are chasing a
signature that "used to work", that is why.

| Who is complaining | What is actually true | What to do |
|---|---|---|
| A nurse cannot sign a report they wrote | Correct and intended. They author and finalize (`report.write`); a clinician signs. | Route the report to a clinician. Do **not** grant `report.sign` to `nurse` — that re-opens the defect for every nurse in every tenant. |
| A tenant_admin cannot sign | Correct and intended. | If they are also a practising doctor, give the **account** the `clinician` role too: `check()` passes on any granting role, so a dual-role person signs normally. |
| A doctor cannot sign | Their account is missing the `clinician` role. | `PUT /admin/users/{sub}/roles`. Confirm with the `signing.denied_role` audit row, which names their full role list. |
| A physician-assistant-style role needs to sign | Not modelled. | This is a **new role** decision with `report.sign` granted, plus a `docs/adr/` amendment — never `nurse` widened. |

Every refusal writes `signing.denied_role` at `sec` severity (naming the
principal and their full role list) and increments
`mdx_signing_denied_total{role}`. The `SigningDeniedByRoleRepeated` alert
fires on >5/hour in a tenant — usually either a wrong role assignment or
a SPA offering a button it should not.

The check is enforced in three places on purpose: the permission matrix
(`libs/auth/perms.py`), the `requires("report.sign", …)` dependency on
every signing route, and `assert_may_sign()` in
`signing_service/signing_authority.py`, which runs in the handler body
**before** any provider is selected or dispatched to. A future route that
forgets the dependency still cannot reach the crypto.

## Operational tunables

| envvar                                | default       | purpose                                             |
| ------------------------------------- | ------------- | --------------------------------------------------- |
| `PUBLIC_VERIFY_RATE_PER_MINUTE`       | 60            | Per-IP rate on `/verify/*`                          |
| `DIIA_BASE_URL` / `DIIA_API_TOKEN`    | unset         | If empty, Дія provider is not wired                 |
| `IIT_HELPER_HEALTH_URL`               | unset         | If empty, ІІТ provider is not wired                 |
| `ENABLE_MOCK_PROVIDER`                | true (dev)    | Production deployments leave false                  |
| `TRUST_STORE_DIR`                     | `infra/trust-store` | Bundle directory                              |
| `TRUST_STORE_INCLUDE_TEST_CA`         | false         | Dev-only; must be false in prod                     |
| `SIGNER_IPN_HMAC_KEY` (hex)           | placeholder   | Rotated yearly (sprint-17 via KMS)                  |
| `PUBLIC_VERIFY_IP_HMAC_KEY` (hex)     | placeholder   | Rotated yearly; old HMACs not back-rotated          |

## Secrets

Sprint-09 ships the HMAC keys via envvars + the master-KEK pattern
introduced in sprint-03. Sprint-17 will wire KMS-backed envelopes.

## Sprint-09 closure

This runbook is the operational contract for the signing surface. Any
playbook step that turns out wrong in practice must be updated in the
same PR as the fix.
