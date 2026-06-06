# Sprint-09 — Implementation Plan (as-built)

## Day 1 — Service skeleton + provider abstraction
- [x] `libs/kep/` package + `SigningProvider` ABC + DTOs
- [x] `MockProvider` with production-refusal
- [x] Test CA scaffolding (auto-generated on first run)
- [x] End-to-end CMS signing/verification test path

## Day 2 — Data model
- [x] Migration `0019_create_signed_envelopes.sql` + .down
- [x] Migration `0020_create_signing_sessions.sql` + .down
- [x] Migration `0021_create_signing_provider_health.sql` + .down
- [x] Migration `0022_create_public_verify_audit.sql` + .down
- [x] Roles: `app_role`, `app_public_verify`, `app_callback_writer`

## Day 3 — Canonicalization + PDF
- [x] `medical_kep.canonicalize` reusing `audit.canonical`
- [x] PDF renderer with embedded canonical JSON + deterministic dates
- [x] CSS-injection defence via Jinja2 autoescape + field clamp
- [x] Round-trip extraction (`extract_embedded_canonical_json`)

## Day 4 — Дія
- [x] `DiiaProvider` (initiate / handle_callback / health)
- [x] Public-keys cache with 1h TTL
- [x] Callback signature verification (PKCS1v15)

## Day 5 — ІІТ + selection
- [x] `IitProvider` (local-helper payload + HMAC callback)
- [x] `select_providers` with 3-strike rule + user choice

## Day 6 — API endpoints
- [x] POST `/signing/sessions` (internal JWT)
- [x] POST `/signing/callbacks/{provider}` (provider-signature)
- [x] GET `/verify/{token}` (public, rate-limited)
- [x] GET `/verify/{token}/pdf` (public, rate-limited)
- [x] Security middleware chain for public surface

## Day 7 — Reaper / health monitor / trust store
- [x] 60s session reaper
- [x] 30s provider health monitor (3-strike hysteresis)
- [x] `scripts/update-trust-store.sh` + GH Actions weekly workflow
- [x] `infra/trust-store/README.md` documenting governance

## Day 8 — Adversarial hardening
- [x] Token format validator (`is_well_formed_token`)
- [x] Rate limit with fakeredis-tested fail-open
- [x] Security headers middleware
- [x] No-info responses for 401/404/410 (no leaking)

## Day 9 — Corner-case PDFs + legal review templates
- [x] PDF template (`report.html.j2`)
- [x] Legal sign-off template

## Day 10 — Docs + sign-off + memory
- [x] ADR-0022 PAdES + embedded JSON
- [x] ADR-0023 Provider abstraction
- [x] ADR-0024 Canonical JSON via JCS
- [x] `docs/architecture/signing.md`
- [x] `docs/runbooks/signing.md`
- [x] `docs/audit/audit-kinds-sprint-09.md`
- [x] SIGN-OFF + RETRO + SPRINT-TODO
- [x] memory entry + MEMORY.md index update
