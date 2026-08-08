# ADR-0002 — Segment taxonomy and the frozen contract enums (spec "ADR-000C")

- **Date:** 2026-08-04
- **Status:** Accepted — **pending clinical counter-signature (AC-S01-B-6)**
- **Deciders:** development agent; clinical advisor sign-off pending

## Context

Rule RC2/FE7 fixes six segment kinds; rule P2 makes the kind the safety
boundary between "established" and "everything else". S01 freezes the enums the
whole product renders, filters, and evaluates on. Enum values are lowercase
snake, wire-final; changes after release go through a v2 contract + ADR.

## Decision

| Enum | Values | Notes |
|---|---|---|
| `SegmentKind` | `evidence, patient_fact, interpretation, uncertainty, missing_info, next_step` | only `evidence` may read as established (P2); ET2: `evidence` ⇒ ≥1 citation, enforced by a Pydantic validator AND a DB CHECK |
| `EvidenceTier` (= strength scale, SC2) | `guideline, systematic_review, rct, observational, other` | tenant provenance is a badge via `SourceAuthority`, not a tier |
| `SourceAuthority` (RR3) | `tenant, national, international, primary_literature, other` | presentation precedence in this order |
| `LicenseClass` | `public_domain, open_license, licensed_redistributable, licensed_internal, restricted` | S02 license register keys on it |
| `FactSource` | `chart, act, user_reported, host_reported` | FU2 context deltas are the last two |
| `FactStatus` | `coded, uncoded, conflict` | `conflict` renders per CS4, never averaged |
| `AnswerStatus` | `ok, insufficient_basis, deflected` | `insufficient_basis` is a first-class state (SC3) |
| `WebTrustTier` | `government, professional_society, guideline_registry, journal, other` | S04 web agent allowlist tiers |
| `QuestionType` | `diagnosis, therapy, dosing, interaction, contraindication, prognosis, etiology, prevention, other` | PICO-ish typing for retrieval planning |
| `FollowUpPriority` / `FollowUpAnswerType` | `high, medium, low` / `boolean, choice, number, quantity, text` | FU1 ranking + structured answers first (FU2) |
| `CheckOutcome` | `passed, warning, failed, not_applicable` | deterministic battery results (CS1) |
| `StageOutcome` | `ok, retried, failed, skipped` | per-stage traces (BE5) |

Also decided here: `PatientFact.field_path` is the grounding unit (CS3b — the
string that `patient_fact_refs` and `consumed_fields` resolve against), and
`deid_required` defaults to **True** (the safe path is the default path).

## Consequences

FE renderers, eval gates, and the DB CHECKs all key on these literals; adding a
value is additive (linter-allowed), removing/renaming is breaking (linter-blocked).

## Alternatives considered

- **SUFHI-style two-way split (supported/unsupported):** rejected — collapses
  patient-fact grounding, gaps, and uncertainty into one bucket the UI cannot
  render distinctly, and FU3's `missing_info` semantics need a first-class kind.
- **Open string kinds with a registry:** rejected — "the unsafe path must not
  compile" requires a closed enum at the schema level.

## Trigger conditions for revisiting

- Eval shows systematic kind confusion between `interpretation` and
  `uncertainty` (the two most adjacent kinds) — revisit with rendering data.
- Clinical advisor review (pending) rejects a value set — v1 is not yet
  released to a tag, so pre-tag corrections are still cheap.
