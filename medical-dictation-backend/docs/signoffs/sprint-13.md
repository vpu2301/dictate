# Sprint 13 sign-off — Structured Anamnesis (typed fields, extraction, ICD-10)

**Date:** 2026-07-23
**Branch:** `S13`
**Migrations:** `0054_create_icd10_codes`, `0055_seed_anamnesis_voice_commands`
**ADR:** ADR-0032 (field-extraction stage) + amendments to ADR-0016 and ADR-0021
**Pipeline:** `nlp-v1.0.0` → **`nlp-v1.1.0`**; cache key `nlp-cache-v2` → `nlp-cache-v3`

---

## What shipped

Typed fields end-to-end. A clinician dictates "пацієнт не курить" and
`smoking_status` fills itself **as a proposal**; they confirm or
correct it; finalize refuses to let a required typed field through
empty; and a diagnosis can never be coded by the machine alone.

| Step | Delivered |
|---|---|
| 01 | `FieldType.CHOICE`/`MULTI_CHOICE`, `ChoiceOption`, options validators, `anamnesis_intake` system template, additive proof over 20 shipped templates |
| 02 | The `field_specific_metadata` key registry, typed constructors in `report_models`, write-path 422s, canonical-bytes + envelope regression guards |
| 03 | `icd10_codes` (0054), idempotent loader, `GET /v1/icd10/search`, one shared ranking SQL for picker + extractor |
| 04 | The `field_extraction` stage, choice/multi-choice matching, ADR-0032, frozen-replay harness |
| 05 | Numeric/date binders over a new stage-artifact channel; ICD-10 diagnosis **proposals** |
| 06 | Typed finalize completeness + the confirmation flag; confirm/override audit |
| 07 | Anamnesis voice commands (0055), three choice operations, the TP/FP corpus |
| 08 | Extraction-quality dashboard + 3 alerts, reconciled audit catalogue, these records |

## The prime directive held

Every guess-shaped decision resolved toward "leave it empty and keep
the prose". Concretely, the extractor emits **nothing** when the
signal is below threshold, when two options conflict on disjoint
evidence, when a negator governs the match, when a `numeric_with_unit`
value has no unit, when two dates are present, or when the ICD-10
lookup times out. Proposals never satisfy a required diagnosis under
**any** configuration — that one is enforced by a parameterised test
over both flag values.

## Defects found and fixed by this sprint's own tests

The corpora earned their keep. Six real bugs, five of them
clinical-safety issues:

1. **"кинув палити" filled nothing** — its second token fuzzy-matched
   `current`'s "палить", so the ambiguity rule killed every "quit
   smoking" utterance. Fixed with overlap subsumption (step 04).
2. **"алергій немає, окрім латексу" read as *no allergies*** —
   silently dropping a latex allergy, the worst error this extractor
   can make. Fixed with contrast markers (step 04).
3. **"прибрати пеніцилін" fired `set_choice`** — "прибрати" (remove)
   is Levenshtein-2 from "обрати" (set), so *remove an allergy* became
   *select it*. Fixed with per-spec exact matching (step 07).
4. **"слід" matched the `slash` command** — a **pre-existing sprint-05
   FP**; "слід призначити…" is everywhere in Ukrainian clinical prose
   and was inserting stray "/" into notes. Fixed (step 07).
5. **An `option_not_found` no-op would have eaten a clinical word** —
   "встановити діагноз поки неможливо" lost its first word. Changed to
   reject; reasons now require positive evidence (step 07).
6. **9 sprint-08 audit kinds were undocumented** — found by the new
   registration guard, documented (step 08).

## Plan-vs-as-built corrections (five)

The step docs described several mechanisms that did not exist. Each was
verified, corrected, and recorded rather than worked around:

| Assumed | Actually |
|---|---|
| NLP context already carries template options | `pipeline.base.TemplateSection` was a 3-field local type; extended additively |
| Normalizers attach structured results to metadata | They emit only `.latency_ms`/`.changed`, and `as_input()` drops metadata — a stage-artifact channel was built |
| Tenant settings exist (`require_patient_on_finalize`) | No tenant-settings store at all; the flag is service config, with the gap named |
| Migration 0011 seeds the command catalogue | 0011 only creates the table; JSON fixtures are authoritative and the seeder wipes per language |
| A sprint-05 TP/FP corpus with targets exists | Only a gate suite; the labeled corpus was built here, measured before/after |

ADR numbering also shifted: the plan's "ADR-0028" is **ADR-0032**
(0028–0031 were taken).

## Evidence

- **Additive proof**: 20 pre-S13 templates validate and dump
  byte-identically to frozen pre-bump fixtures.
- **Signing regression**: pre-S13 canonical bytes unchanged; mock-KEP
  envelopes over old content still verify post-bump.
- **Frozen replay**: `nlp-v1.0.0` fixtures replay through the six-stage
  pipeline, `nlp-v1.1.0` through seven; identical input with no typed
  sections produces identical bytes across both versions.
- **Command corpus**: recall 1.0 / **0 false positives** both before
  and after seeding the four new specs (18 positives, 12 negatives).
- **ICD-10 search**: p95 **0.86 ms** on the 239-code fixture, **1.73 ms**
  on a 12 189-code synthetic expansion — against a 50 ms budget.
- **Live end-to-end**: dictation → extraction → `field_specific_metadata`
  on a stored draft; typed finalize blocking then passing; both audit
  kinds in the hash chain with slug/code-only payloads; voice commands
  resolving to `set_choice`/`add_choice`/`remove_choice`.
- **Observability**: override-rate panel populated from real local
  traffic (`choice` = 0.385 from 4 confirms + 2 overrides); all three
  alerts loaded; the override rule fire-tested to `pending` at 0.341.

### Suite counts (final)

| Package | Tests |
|---|---|
| nlp-service | 320 |
| report-service | 200 |
| report_models | 45 |
| template_models | 58 |
| medical_kep | 42 (+6 skipped) |
| signing-service | 45 |

`make ci`, `check-rls` (33 tables), `check-erasure-fanout`,
`lint-imports` (17/17), `promtool check rules` — all green.

---

## Carry-overs — every open question has an owner

| # | Item | Owner | Why it is not an engineering decision |
|---|---|---|---|
| 1 | **Acquire the full МКХ-10-АМ table** | clinical lead + ops | Only a 239-code hand-checked fixture ships. No official МОЗ/НСЗУ download under clear redistribution terms was found; the AM base is licensed. Until then codes outside the fixture are un-codeable — clinicians dictate them as prose. Nothing is mis-coded. `docs/runbooks/icd10.md` |
| 2 | **`anamnesis_intake` wording + allergen list** | clinical content lead + linguist | Engineering authored plausible clinical content; option labels and the allergen set need clinical review before pilot use. |
| 3 | **Voice-alias coverage** | clinical content lead | Recall is bounded by aliases **by design** — the extractor never compensates by matching looser. The override-rate panel converts misses into authoring tasks. |
| 4 | **May a tenant auto-promote ICD-10 proposals at finalize?** | clinical lead + DPO | The sprint doc's flag was ambiguous. Implemented conservatively (messaging only, never auto-promotion), because auto-promotion puts a machine-chosen diagnosis into a signed record. Reversing this is contained to one function. |
| 5 | **Compound measurements (BP)** | clinical content lead | `numeric_with_unit` holds one value; BP normalizes to `140/90` and binds nothing. Model as two sections, or fund a compound field type. Never special-cased silently. |
| 6 | **Tenant-settings mechanism** | tech lead | The confirmation flag is platform-wide because no per-tenant store exists. `validate_finalize` already takes it as an argument. |

### Accepted deviations

- **`icd10.searched` is metrics-only.** A hash-chained row per
  keystroke is chain pollution; the S10 suggest path set this
  precedent. Rationale in `docs/audit/event-kinds.md`.
- **`anamnesis.field.extracted` is aggregated per finalized report**,
  not per utterance, for the same reason.
- **Local edit-distance and token-scoring instead of rapidfuzz.** The
  stage's output is frozen by `pipeline_version`; a dependency version
  must not be able to move clinical confidence numbers.
- **`set_choice` on a multi-select replaces the whole selection** —
  predictability over cleverness; cheap to revisit with pilot data.

### Explicitly not in scope

No ICD-10 selection by voice. `diagnosis.capture` only marks where
dictated diagnosis text begins.

---

## Sign-off

| Role | Confirms |
|---|---|
| Tech lead | Gates green; ADR-0032 + amendments recorded; five plan-vs-as-built corrections documented |
| Clinical lead | ⏳ carry-overs 1–5 |
| DPO | ⏳ carry-over 4 (billing angle); audit payloads verified slug/code-only |
| ML/MLOps | Replay determinism per frozen version; threshold change treated as a pipeline change |
