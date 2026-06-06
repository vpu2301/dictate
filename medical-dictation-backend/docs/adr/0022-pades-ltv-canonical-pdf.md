# ADR-0022 — PAdES-LTV with embedded canonical JSON

- Status: accepted
- Date: 2026-05-14
- Sprint: 09
- Deciders: tech lead, security lead, legal counsel, DPO

## Context

Sprint-09 attaches legal weight to clinical reports. Three things are
simultaneously true:

1. Ukrainian Law 2155-VIII requires QES (Qualified Electronic
   Signature) for medical record signing, with retention of the
   cryptographic evidence for ≥ 25 years.
2. The signed artifact must be **human-readable** (a clinician,
   inspector, or judge can read the report without specialised
   software).
3. The signed artifact must be **structurally verifiable** (an
   automated verifier can re-check the signature, document hash, and
   certificate chain without parsing the human-readable rendering).

Three candidate formats:

- **JSON-only** with an external rendered PDF. Verifies easily but
  fails the human-readable requirement; the rendered PDF can drift
  from the signed JSON.
- **CAdES detached**. Signs the canonical JSON bytes; rendered PDF
  carried separately. Same drift risk.
- **PAdES** signing a PDF that embeds the canonical JSON as an
  attachment. The signature covers the PDF (human-readable rendering)
  AND the JSON (structured payload) is byte-binding via the embedded
  attachment.

## Decision

Adopt **PAdES-LTV with embedded canonical JSON attachment**. Each
sprint-09 signed envelope is a PDF that:

- Renders the report deterministically (sprint-09 day-3 PDF pipeline).
- Embeds `canonical.json` as a PDF `/EmbeddedFile`. The bytes match
  `report_models.canonical_content_bytes(...)`.
- Carries the PAdES signature in `/ByteRange`.
- Includes the TSA token (qualified timestamp from КНЕДП TSA).
- Optionally upgrades to PAdES-LTV by embedding OCSP responses for
  each cert in the chain.

The canonical JSON shape is locked by ADR-0024 + sprint-08's
`ReportContent` schema. The PDF rendering is locked by the WeasyPrint
container image digest pinned at build time.

## Why not CAdES detached

CAdES detached signs the *canonical bytes only*. A human looking at a
rendered PDF later cannot trivially confirm "this is what was
signed". With PAdES, the bytes you read are inside the bytes that
were signed.

The cost: the rendered PDF is part of the signed artifact, so
re-rendering with a different WeasyPrint version produces different
bytes. We mitigate by pinning the container image digest and by the
day-3 determinism test (100 renders of the same input produce 100
byte-equal PDFs).

## Consequences

Positive:
- Human + machine verifiable from one artifact.
- 25-year LTV preserved by storing OCSP + TSA in the envelope at
  signing time.
- Verifier-side complexity is bounded — extract `canonical.json`,
  verify it against the JSON projection, verify the PAdES signature
  against the trust store.

Negative / accepted:
- Render determinism is now load-bearing. A WeasyPrint upgrade is a
  release-blocker until the determinism test passes.
- The PDF carries the full report bytes — file size is a function of
  the report. Acceptable: clinical reports are < 50 KB after compression.
- ICAO's "ETSI EN 319 142-1 PAdES-LTV" profile is rigorously defined;
  we promise conformance but the validator we ship in
  `libs/kep/verify.py` is sprint-09's best-effort baseline, not yet a
  certified conformance tool.

## Links

- ADR-0023 (Provider abstraction).
- ADR-0024 (Canonical JSON via JCS).
- `libs/kep/src/medical_kep/pdf_renderer.py`.
- `libs/kep/src/medical_kep/envelope.py`.
- Sprint-09 spec §2.4, §2.5.
