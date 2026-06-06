# Sprint 09 — Sign-Off

**Sprint dates:** 2026-05-27 → 2026-06-09
**Status:** ✅ Code complete; pilot env Дія/ІІТ + legal counsel review pending.

## Scope delivered

- ✅ **Day 1 (provider ABC + mock)**: `medical_kep.SigningProvider` ABC,
  `MockProvider` with production-refusal, test-CA fixtures.
- ✅ **Day 2 (data model)**: migrations 0019-0022 (`signed_envelopes`,
  `signing_sessions`, `signing_provider_health`, `audit.public_verify_audit`),
  RLS + dedicated public-verify role.
- ✅ **Day 3 (canonicalization + PDF)**: JCS via `audit.canonical`,
  WeasyPrint+pypdf with embedded JSON + deterministic dates.
- ✅ **Day 4 (Дія)**: `DiiaProvider` with callback signature verify +
  envelope fetch; httpx-based; 5xx → ProviderTransientError.
- ✅ **Day 5 (ІІТ + selection)**: `IitProvider` (local-helper + HMAC
  callback), `select_providers` (health-aware fallback, 3-strike rule).
- ✅ **Day 6 (API)**: POST `/signing/sessions`, POST
  `/signing/callbacks/{provider}`, GET `/verify/{token}`,
  GET `/verify/{token}/pdf`; security middleware chain for /verify.
- ✅ **Day 7 (reaper + health + trust store)**: 60s reaper, 30s health
  monitor, weekly TSL refresh script + GH Actions workflow.
- ✅ **Day 8 (adversarial)**: token format validator rejects
  traversal / SQLi / format-string; rate-limit via fakeredis-tested
  module; security headers (HSTS, CSP, X-Frame-Options, no-store).
- ✅ **Day 9 (corner-cases templates)**: PDF template
  (`libs/kep/src/medical_kep/templates/report.html.j2`) wired; legal
  sign-off template (`docs/signoffs/sprint-09-legal.md`).
- ✅ **Day 10 (docs)**: ADR-0022/0023/0024, runbook, architecture doc,
  audit-kinds-sprint-09, sign-off + retro + memory entry.

## Tests

- `libs/kep` unit: **23/23 passing**.
- `signing-service` unit: **13/13 passing**.
- Cumulative sprint 03–09 unit tests: **280 passing**.

## Out of scope (deliberate)

- Additional providers beyond Дія, ІІТ, mock.
- Bulk signing.
- Re-signing on cert refresh (LTV handles).
- Mobile-native signing flows.
- Anonymous public signing.

## Sign-offs

| role            | name      | date      |
| --------------- | --------- | --------- |
| Tech lead       | _pending_ | _pending_ |
| Security lead   | _pending_ | _pending_ |
| DPO             | _pending_ | _pending_ |
| Legal counsel   | _pending_ | _pending_ |
| SRE/DevOps      | _pending_ | _pending_ |
| Frontend lead   | _pending_ | _pending_ |
| Clinical lead   | _pending_ | _pending_ |

## Known follow-ups

1. Real Дія test-env signing demo (needs allocated Дія test credentials).
2. Real ІІТ smart-card test (needs hardware).
3. Legal counsel review (day-9 spec).
4. Production trust-store bundle (sprint-09 ships an empty
   `infra/trust-store/` — operations populates with the live КНЕДП
   roots).
