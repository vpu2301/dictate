# ADR-0023 — Signing provider abstraction

- Status: accepted
- Date: 2026-05-14
- Sprint: 09
- Deciders: tech lead, security lead, SRE/DevOps lead

## Context

Sprint-09 integrates two qualified signing providers (Дія.Підпис and
ІІТ) and ships a mock for CI. Both providers operate independently of
us — their APIs, callback shapes, signature formats, and outage
patterns differ. If our services embed Дія specifics directly,
swapping providers (or adding PrivatBank in sprint-17) requires deep
surgery.

## Decision

Define one ABC — `medical_kep.SigningProvider` — with five methods
(`initiate`, `handle_callback`, `health`, `aclose`, plus the `name`
class attribute). All concrete providers implement this ABC. The
service layer (signing-service) depends on the ABC only; concrete
classes are wired at boot via `providers.build_registry()`.

The abstraction commits to:

- **Initiate** returns either `redirect_url` (mobile flow) OR
  `local_helper_payload` (smart-card helper flow). Exactly one is
  non-None per provider; both are part of the DTO so the FE can drive
  either uniformly.
- **Callback** is signature-validated by the provider implementation;
  the service layer does not see the raw signature bytes.
- **Envelope parse** is provider-agnostic (`libs/kep/envelope.py`)
  because both Дія and ІІТ ultimately emit CMS-SignedData.
- **Health** is a lightweight probe with a 3-strike rule for
  hysteresis (sprint-09 day-7 `signing_provider_health`).

What the abstraction does NOT abstract:

- The trust store. Trust anchors are sprint-09 system-wide, not
  per-provider. Adding a provider means adding its issuing КНЕДП CA
  cert to the bundle (PR-gated; see `infra/trust-store/README.md`).
- The canonical JSON shape. Canonicalisation is provider-agnostic
  (ADR-0024); every provider signs the same bytes.

## Consequences

Positive:
- Provider selection (sprint-09 day-5) is data-driven: health-aware
  fallback works without touching service code.
- A new provider is a sub-200-line addition (one class + one config
  + registry plumbing).
- The mock provider exercises the full verification path against the
  same code that Дія/ІІТ envelopes flow through.

Negative / accepted:
- Provider-specific quirks (e.g. ІІТ's WebUSB helper protocol
  version) leak into the DTO via `local_helper_payload: dict`. We
  picked a dict over fragmenting the DTO into N variants because the
  FE just forwards the payload to the helper.
- Production refusal of the mock provider lives in two places (the
  mock's constructor + `enable_mock_provider` flag). Both are
  load-bearing; removing either weakens the guarantee.

## Links

- `libs/kep/src/medical_kep/provider.py` (ABC).
- `libs/kep/src/medical_kep/diia_provider.py`.
- `libs/kep/src/medical_kep/iit_provider.py`.
- `libs/kep/src/medical_kep/mock_provider.py`.
- `libs/kep/src/medical_kep/selection.py`.
- Sprint-09 spec §2.2, §2.7.
