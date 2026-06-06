# Signing architecture (sprint-09)

The legal-weight surface. Sprint-09 attaches qualified electronic
signatures (QES) per Ukrainian Law 2155-VIII to clinical reports.

## Topology

```
                          ┌────────────────────────────┐
   FE  ─── POST /signing/sessions ─►  signing-service  │ ── tenant_connection ─► reports.* (sprint-08)
                          │   (internal JWT)            │
                          │                             │
                          │   provider.initiate()       │
                          │   ─► Дія / ІІТ / mock       │
                          ▼                             │
                       redirect_url / local_helper      │
                          │                             │
   user signs on phone / smart card                     │
                          │                             │
                          ▼                             │
     provider ──POST /signing/callbacks/{provider}──►   │
                          │   (provider-signature       │
                          │    validated by provider    │
                          │    impl)                    │
                          │                             │
                          │  parse + verify envelope    │
                          │  against trust store        │
                          │                             │
                          ▼                             │
                  insert signed_envelopes ◄─────────────┘
                          │
                          │  mint verification_token
                          ▼
   anyone ─── GET /verify/{token} (no auth, rate-limited) ───► signing-service
```

## Provider abstraction (ADR-0023)

`medical_kep.SigningProvider` ABC. Concrete implementations:

- `DiiaProvider` (mobile flow; callback signature via Дія public key).
- `IitProvider` (smart-card helper flow; callback HMAC).
- `MockProvider` (CI/dev; refuses to run in production).

Selection:

- Default Дія; fall back to ІІТ if Дія unhealthy.
- 3-strike rule on health flip (no flapping).
- User can choose either if both healthy.
- Mock is excluded from public selection unless `allow_mock=True`.

## Canonical bytes (ADR-0024)

Every envelope signs the **same canonical shape** regardless of
provider:

- `medical_kep.canonicalize_report(CanonicalReportInput)`.
- Returns `(canonical_bytes, sha256_hex)` — RFC 8785 JCS via the
  `rfc8785` library, shared with sprint-02 audit chain.
- The canonical shape is the legal contract; `CANONICAL_VERSION = "1.0"`
  is locked.

## PDF rendering (ADR-0022)

- Deterministic WeasyPrint render (sprint-09 day-3) with the canonical
  JSON embedded as a PDF `/EmbeddedFile`.
- Same input → same bytes.
- CSS-injection defence: untrusted strings clamped + Jinja2 autoescape;
  no inline `<style>` driven by user input.

## Envelope parsing + verification

`libs/kep/envelope.py` parses CMS SignedData (CAdES variants) and the
PDF wrapper (PAdES variants). Output is `ParsedEnvelope` consumed by
`verify_envelope`:

1. Document hash binding.
2. Cert chain terminates in trust store.
3. Cert validity window contains signed_at.
4. TSA token present for declared LTV format.
5. OCSP responses present for qualified envelopes (warning, not fatal,
   in sprint-09; sprint-17 may tighten).
6. Test-anchored chains are NEVER qualified, regardless of cert flags.

## Data model

| table                          | purpose                                  |
| ------------------------------ | ---------------------------------------- |
| `signed_envelopes`             | The legal artifact + cert chain (RLS)    |
| `signing_sessions`             | Lifecycle row (RLS)                      |
| `signing_provider_health`      | Global; 30s health monitor               |
| `audit.public_verify_audit`    | Global; IP HMAC; no tenant scope         |

## Public /verify

- Unauthenticated. The verification token (16 random bytes,
  base64url, 22 chars) is the secret.
- Rate-limited per-IP, 60 req/min default, fail-open on Redis errors.
- Security headers: HSTS, X-Frame-Options DENY, CSP locked-down,
  X-Content-Type-Options nosniff, no-store cache, no Set-Cookie.
- Response shape is a fixed enum projection of `VerificationResult` —
  no leaking of raw verifier error strings.
- `/verify/{token}/pdf` returns the signed PDF bytes with
  `Content-Disposition: attachment; filename=<sanitised>.pdf`.

## Audit

Two paths:

- **Tenant-scoped chain** (`audit.events`, sprint-02 hash-chained):
  session.initiated / expired / rejected / failed / envelope.persisted
  / callback_signature_invalid / provider.health_changed.
- **Global verify stream** (`audit.public_verify_audit`): every
  `/verify/*` call. IP HMACed with system key. No tenant_id.

## Hand-offs

- Sprint-08 (Reports): `POST /v1/reports/{id}/sign` placeholder
  becomes a thin wrapper that calls signing-service. Status transitions
  `finalized → signed → amended` move forward when the envelope persists.
- Sprint-11 (Patients): shares HMAC-of-IPN pattern with this sprint.
- Sprint-17 (FHIR): `Provenance.signature` / `Composition.signature`
  reference `signed_envelopes`.
