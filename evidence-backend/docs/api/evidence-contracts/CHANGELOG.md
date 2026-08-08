# Evidence contracts changelog

One entry per contract per released version. Policy (rule RC1): additive
changes bump the minor version; breaking changes require a new major version,
a new `*.vN.schema.json` file, and an ADR. `scripts/contracts/lint_additive.py`
enforces this against the last `evidence-contracts-v*` git tag.

## v1.0 — 2026-08-06 (EVA-S04, additive)

Two new contracts, no changes to released shapes (minor-compatible; the
version string stays `1.0` until the first tag exists — see the release
procedure note at the bottom).

| Contract | Notes |
|---|---|
| `answer_stream_event` | The `POST /answers` SSE wire union (header / summary_segment / detail_segment / source / late_source / done / error). Payload-matching validator: an event carrying the wrong payload does not construct. Nested: `AnswerStreamHeader`, `AnswerStreamDone`, `AnswerStreamError`, `AnswerMode` |
| `triage_decision` | Intake triage outcome + stable reason codes (`emergency`, `self_harm`, `personal_advice`, `out_of_scope`, `unsafe_request`) with locale-resolved safe messaging |

One additive enum change to a released contract:

| Contract | Change |
|---|---|
| `web_source_ref` | `WebTrustTier` gains `international_organization` (WHO/ECDC/EMA). Additive per RC1 — existing values and their meanings are untouched; consumers that don't know the value fall back to "other" rendering |

## v1.0 — 2026-08-04 (EVA-S01, initial release)

All fourteen contracts released at `1.0`:

| Contract | Notes |
|---|---|
| `answer_envelope` | RC1 envelope; ET2 + citation→source integrity + 256 KB cap validators |
| `segment` | RC2; six kinds (ADR-0002); `strength` reuses `EvidenceTier` |
| `patient_snapshot` | RC3; `field_path` grounding unit; `deid_required` default true |
| `clinical_intent` | RC3; de-identified concepts only (QS1 input) |
| `followup` | RC3; `blocking` default false (FU3) |
| `answer_provenance` | ET1 record shape (persisted append-only) |
| `stage_trace` | BE5 per-stage trace |
| `web_source_ref` | S04 consumer; trust tiers per ADR-0002 |
| `document`, `document_version`, `chunk`, `corpus_snapshot` | S02 consumers; RR2 metadata fields |
| `connector_descriptor`, `evidence_passage` | S03 consumers; connector interface freeze happens in S03 |

Release procedure: merge the sprint PR, then tag `evidence-contracts-v1.0.0`
on the merge commit — the tag is the linter's comparison baseline.
