# Sprint 09 — Retro

## What went well

- **Provider ABC held up across Дія and ІІТ.** Two very different UX
  flows (mobile vs smart card) fit cleanly into the same `initiate +
  handle_callback + health` shape. The DTOs were the right level of
  abstraction.
- **Reusing sprint-02 `audit.canonical` for signing canonicalisation.**
  One implementation of RFC 8785; one place to audit. Sprint-02
  paid this dividend.
- **Mock provider as a real CMS path.** The CI test exercises the
  same parser + verifier code that production envelopes will hit.
  Caught two CMSAttribute-construction bugs early.
- **Production-refusal in two places.** Mock's constructor refuses
  + `enable_mock_provider` flag in config. Belt-and-braces.
- **Public `/verify` is genuinely small.** The response shape is
  locked, no error-string leakage, security headers on every response.

## What was hard

- **asn1crypto CMSAttribute is finicky.** Passing `ObjectIdentifier`
  objects to a `ContentType` field that expects a string took two
  passes to fix.
- **PAdES vs CAdES dual parsing.** The envelope module attempts CMS
  first, falls through to PDF. Sprint-09 mock only exercises CMS; real
  PAdES extraction needs the day-9 corner-case test corpus on real
  envelopes.
- **Trust-store update workflow is high-stakes.** The script + Slack
  + PR-gating chain is correct; the day-7 dev run was clean; but the
  first real TSL change will need a careful eye.
- **Test CA fixture lived in the wrong directory.** First version
  computed `parents[3]` and put fixtures under `libs/tests/` which
  caused `uv sync` to fail. Fixed; could be a CI lint candidate.

## What we would change

- **Add a Dockerfile lint** that fails if `test-ca-bundle.pem` ends up
  in the production image. Currently relies on COPY scope being
  correct; one mis-globbed COPY breaks it.
- **Wire the canonical_json into the envelope persist.** Sprint-09
  passes `canonical_json={}` because the canonicalisation entry point
  lives in the report-service caller, which sprint-10 will integrate.
  The day-7 lint should add a NOT NULL CHECK on `canonical_json` once
  that wire is complete.

## Decisions taken

- PAdES with embedded canonical JSON is the legal artifact format
  for every signed clinical resource going forward (ADR-0022).
- Provider abstraction is the contract; new providers slot in without
  touching service code (ADR-0023).
- RFC 8785 JCS is the canonicalisation across audit + signing (ADR-0024).
- Trust-store updates are PR-gated, never auto-applied.

## Carry-over items

- Sprint-10: integrate sprint-08 amendment flow with
  `signing-service`. The placeholder 501 on `/v1/reports/{id}/sign`
  becomes a thin wrapper.
- Sprint-11: HMAC-of-IPN pattern shared with patient identifier.
- Sprint-17: FHIR `Provenance.signature` populated from
  `signed_envelopes`.
- Sprint-17: KMS-backed envelope for HMAC keys + cert chain storage.
