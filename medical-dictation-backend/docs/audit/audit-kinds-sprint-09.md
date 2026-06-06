# Audit kinds — Sprint 09 additions

Appends to the canonical catalogue in `docs/audit/audit-kinds.md`.

## Tenant-scoped (hash-chained `audit.events`)

| kind                                       | emitter             | payload keys                              |
| ------------------------------------------ | ------------------- | ----------------------------------------- |
| `signing.session.initiated`                | signing-service     | `provider`, `resource_type`, `resource_id`, `resource_version_id` |
| `signing.session.expired`                  | reaper job          | `reason='reaper_ttl'`                     |
| `signing.session.rejected`                 | callback handler    | (provider rejected by user)               |
| `signing.session.failed`                   | callback handler / reaper | `reason`                            |
| `signing.session.callback_signature_invalid` | callback handler  | `provider`, `reason` (severity sec)       |
| `signing.envelope.persisted`               | callback handler    | `provider`, `verification_token`, `is_qualified` |
| `signing.provider.health_changed`          | health monitor      | `provider`, `healthy`, `last_error`, `latency_ms` |

## Global (`audit.public_verify_audit`, IP-HMAC, no tenant)

| kind                                  | emitter         | columns                                       |
| ------------------------------------- | --------------- | --------------------------------------------- |
| `signing.envelope.verified_public`    | verify route    | `verification_token`, `requestor_ip_hmac`, `result`, `bytes_returned` |
| `signing.envelope.pdf_fetched_public` | verify route    | same shape                                    |

## Notes

- The signature-invalid event is severity `sec` — pages the security
  lead via `monitoring/prometheus/sprint-09-alerts.yml`.
- The public-verify stream uses HMAC-of-IP with a yearly-rotated key.
  Old HMACs are NOT back-rotated; cross-year linkability is
  intentionally lost so privacy decays with time.
- `signing.provider.health_changed` events are emitted under a fixed
  "system" tenant id (sprint-17 may move to a dedicated global stream).
