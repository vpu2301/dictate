# ADR-0032 — The `field_extraction` pipeline stage (sprint 13)

**Date:** 2026-07-22
**Status:** Accepted
**Deciders:** tech lead, clinical lead, ML/MLOps lead

> **Numbering note.** The sprint-13 plan called this "ADR-0028". By the
> time sprint 13 started, 0028–0031 were taken (privacy-ops deployment
> and the notifications subsystem), so this is **ADR-0032**. References
> to "ADR-0028" in sprint-13 step docs mean this ADR.

---

## Context

Sprint 06 introduced typed template fields; sprint 13 makes them fill
themselves. The clinician dictates "пацієнт не курить" and
`smoking_status` should offer `never` — as a **proposal**, never as a
decided fact.

The sprint-05 pipeline was six stages: `voice_commands → punctuation →
number_norm → date_norm → abbreviation → confidence`. That order is a
contract (byte-equal replay under a frozen `pipeline_version`), so
inserting a stage is an ADR-level event.

## Decision 1 — Canonical order, with `field_extraction` sixth

```
voice_commands → punctuation → number_norm → date_norm
              → abbreviation → field_extraction → confidence
```

**After `abbreviation`**: extraction reads fully normalized text.
Numbers, dates and expanded abbreviations are already applied, so an
alias like "ішемічна хвороба серця" matches whether the clinician said
the abbreviation or the expansion. Extracting earlier would mean
matching against text that is still changing.

**Before `confidence`**: the confidence stage annotates the *final*
text. Extraction adds no text — it is text-neutral by construction
(asserted in tests: the next stage receives byte-identical input) — so
placing it before `confidence` costs nothing and keeps `confidence`
last, where it belongs.

`pipeline_version` bumped **`nlp-v1.0.0` → `nlp-v1.1.0`**, and the
idempotence-cache key version `nlp-cache-v2` → `nlp-cache-v3` (typed
sections now participate in the key: identical text against different
option sets must never share a cache entry).

## Decision 2 — `runs_on_partials = False`

Partial text is unstable: mid-sentence, "не" may not have arrived yet,
so a partial-fed extractor would propose `current`, then flip to
`never` a word later. Proposals that flicker while a clinician is
speaking are worse than proposals that appear once, at the end. Finals
only.

## Decision 3 — Delivery path: `StageOutput.metadata`, not an FE `Operation`

The step doc left this open pending exploration. As-built findings:

- `dictation-service`'s `NlpClient.process_final` exists but **has no
  caller** — the streaming dictation → nlp wiring was never built.
- The batch path (`asr-service → /nlp/process/batch`) does not send
  `template_sections` and its response has no per-segment `metadata`.
- The one **built** draft-assembly path is report-service's
  `POST /v1/reports/from-transcript`.

So: the stage writes `field_extraction.fields` —
`{section_key: metadata}` — onto `StageOutput.metadata`, which
`/nlp/process` already returns verbatim in its deterministic
`metadata` body. **report-service** calls nlp-service at draft
assembly (`domain/field_extraction_client.py`) and writes the result
into `ReportSection.field_specific_metadata`.

report-service is the right caller because it is the only service
holding both halves: the template (with its options) and the draft.
The call is **fail-open** — a timeout or non-200 yields no proposals
and the draft is created with prose only. Losing a proposal costs one
dropdown; failing the draft costs the dictation.

An FE `Operation` was rejected: operations mutate editor state, and a
proposal is not a mutation — it is data attached to a section that the
clinician may accept. When streaming section-aware dictation is wired
(sprint 14+), it will consume the same `metadata` key.

## Decision 4 — Matching rules (the clinical-safety core)

Threshold **0.8** (`MDX_NLP_EXTRACTION_CONFIDENCE_THRESHOLD`,
pilot-tunable). Below it: **no entry at all** — absence, not an empty
object. The prose always survives.

1. **Per-token Levenshtein ≤ 1** absorbs Ukrainian case endings
   ("куріння"/"курінню").
2. **Short-token guard** — tokens under 4 characters must match
   exactly. Without it "не" fuzzy-matches "ні" and "на", and a single
   edit inverts the meaning of a clinical statement. Load-bearing.
3. **Negation guard** — a negator (`не`, `ні`, `без`, `заперечує`,
   `немає`…; en `no`, `not`, `denies`…) within 2 tokens before a match
   blocks it, *unless* the matched phrase carries the negator itself
   (aliases like "не палить" legitimately contain one; otherwise
   negative options would be unfillable).
4. **Contrast markers cancel negation** — `окрім`, `крім`, `але`,
   `except`, `but`… Found during implementation: "алергій немає, окрім
   латексу" was being read as *no allergies*, silently dropping a
   latex allergy. Missing a real allergy is the most dangerous error
   this extractor can make, so the negation guard stops at these words.
5. **Overlap subsumption** — when a longer phrase match overlaps a
   shorter one, the longer wins and the shorter is discarded. Also
   found during implementation: "кинув палити" matches `former` over
   two tokens while its second token alone fuzzy-matches `current`'s
   "палить". Those are the same words read two ways, not two clinical
   claims. Without this rule every "quit smoking" utterance looked
   ambiguous and filled nothing.
6. **Ambiguity ⇒ empty** — if two *different* options clear the
   threshold on **disjoint** evidence, nothing is selected. Competing
   clinical signals are not a coin flip. Order-independent (tested
   both ways).
7. **`multi_choice`**: every option above threshold, deduped; an
   explicit `none_known` is dropped when a positive finding also
   matched. Confidence reported is the weakest member's — a set is
   only as certain as its least certain element.

Recall is bounded by alias coverage **by design**. The template
authoring guide is where recall improves; the extractor never
compensates by matching more loosely. Override rate is the quality
signal (step 08's dashboard).

## Decision 5 — Local edit distance, not rapidfuzz

The step doc named rapidfuzz. We use a local bounded Levenshtein
(~20 lines, exact) instead: the stage's output is frozen by
`pipeline_version`, and a dependency upgrade that changed edit-distance
edge semantics would silently alter the replay of historical sessions.
A determinism contract should not depend on a third-party version
range. `rapidfuzz` remains fine where determinism is not contractual
(autocomplete).

## Replay contract

Byte-equal replay is guaranteed **per `pipeline_version`**, and the
test harness now maps a version to the stage list that produced it:
`nlp-v1.0.0` fixtures replay through the six-stage pipeline,
`nlp-v1.1.0` through the seven-stage one. Both sets are committed
(`services/nlp-service/tests/fixtures/replay/`).

The stage emits **nothing at all** when the context carries no typed
sections, so a pre-sprint-13 caller's output is byte-identical across
the two versions — proven by fixtures with the same input text under
both versions producing identical encoded bytes.

`punctuation` is excluded from the replay harness: it is an ML model
pinned by revision (`docs/models/PINS.md`), not by this contract —
the same exclusion the sprint-07 eval harness makes.

## Consequences

- **ICD-10 reloads** (step 03) are pipeline-affecting events of this
  same class: they change what the step-05 diagnosis extractor can
  propose. Procedure in `docs/runbooks/icd10.md`; replay fixtures pin
  the committed ICD-10 fixture table, not prod's.
- **Threshold changes** change extraction output; treat a change like
  a pipeline change and argue it from the override-rate dashboard.
- **Audit**: no per-utterance event. `anamnesis.field.extracted` is
  emitted **aggregated at session finalize** (step 08 registers it) —
  a hash-chained row per utterance would be chain pollution, the same
  reasoning as the ICD-10 search path being metrics-only.
- **Metrics**: `mdx_nlp_field_extraction_seconds`,
  `mdx_field_extraction_total{field_type, outcome}` where outcome is
  `filled | empty | ambiguous | no_options` — the empty-vs-ambiguous
  split is what tells "nothing was said" from "we heard a conflict".

## Trigger conditions for revisiting

- Override rate above ~20% for any field type → alias coverage or
  threshold review (data, not intuition).
- Streaming section-aware dictation landing (sprint 14+) → the same
  metadata key gets a second consumer; revisit whether the active
  section should be explicit in `ProcessingContext` rather than
  callers scoping by which sections they send.

---

## Appendix (sprint 13, step 05) — numeric/date binders + diagnosis proposals

### The artifact channel (an as-built correction)

Step 05's plan assumed the sprint-05 normalizers "attach their
structured results to stage metadata". They do not: `NumberNorm` and
`DateNorm` emit only `.latency_ms` and `.changed`. Worse,
`StageOutput.as_input()` **drops `metadata`**, so before this sprint
there was *no channel at all* for one stage to hand structured data to
a later one.

So sprint 13 adds one: `NumericArtifact` / `DateArtifact` tuples on
`StageInput`/`StageOutput`, threaded and accumulated by the
orchestrator. Two properties make this safe:

- **Additive and defaulted** — every pre-S13 stage and test that
  constructs a `StageInput` is unaffected.
- **Internal only** — the orchestrator drops artifacts before
  encoding, so they never reach the response body or the cache and
  therefore *cannot* change replay bytes.

Each normalizer reports artifacts by reading **its own canonical
output**, using the unit vocabulary imported from itself
(`number_norm_uk._UNITS`) and the per-request separators it was handed.
The binder consumes that list and parses nothing. Tests enforce the
separation: the binder may not reference normalizer internals, carry
numeral or unit vocabulary, or do date arithmetic.

Why this matters: spoken-numeral logic ("сто сорок" → 140) and
relative-date resolution ("три дні тому" → ISO) each live in exactly
one place. A binder that re-derived either would drift the first time
a normalizer changed.

### Binder rules and confidence constants

| Constant | Value | Why |
| --- | --- | --- |
| `LABELLED_CONFIDENCE` | 0.9 | the section's name/alias sits next to the value — real evidence of intent |
| `SOLE_CONFIDENCE` | 0.8 | exactly the threshold, so a clean single-measurement section binds; lower would make the common case unfillable, higher would claim evidence we lack |
| `DATE_CONFIDENCE` | 0.9 | the normalizer only emits ISO forms it actually recognised as dates |
| `LABEL_WINDOW` | 4 tokens | how far from a value a label may sit |

Numeric binding picks the **nearest** label rather than treating
"labelled" as a boolean: in "маса 80 кг, температура 37,2 °C" both
values fall inside a boolean window of the word "температура", so a
boolean rule would call both labelled and give up. Nearest-wins picks
the value the clinician attached; an exact tie still falls through to
the ambiguity rule (empty).

A value with **no unit** never binds to `numeric_with_unit`: that is
not a measurement, and inventing the unit is precisely the fabrication
this sprint refuses. Several unlabelled values ⇒ empty. Several dates
⇒ empty (picking one would silently misdate a clinical record).

Rule (1) of the step's plan — match the section's *declared* expected
unit — is **dormant**: as-built `TemplateSection` carries no unit hint.
The code documents where it slots in when a template gains one.

### Compound measurements — a stated limitation, not a special case

Blood pressure normalizes to `140/90`, which is not a single numeric
value, so a `numeric_with_unit` section dictated as BP binds
**nothing**. This is deliberate and tested. Supporting BP needs either
two numeric sections in the template or a future compound field type;
special-casing "/" to invent a single value would be a clinical error
wearing a convenience costume. Recorded in the authoring guide and the
sprint sign-off.

### Diagnosis proposals

Candidate splitting is conservative: only explicit delimiters
(`;`, `,`, `+`). Bare "і"/"та" are **never** split points — "нудота і
блювання" is one diagnosis (R11) and we cannot distinguish it from a
two-diagnosis sentence without parsing. Over-splitting fabricates
diagnoses that were never dictated.

Scoring is **coverage of the candidate**, not symmetric overlap: ICD-10
displays are long formal titles while clinicians dictate short ones, so
a symmetric measure divides by the long title and puts every realistic
utterance below any sane threshold — the extractor would propose
nothing, ever. Single-token candidates are damped (×0.75): one word is
weak evidence of a diagnosis. Rank decay (×0.9 per position) keeps the
lookup's own ordering meaningful. Caps: 3 per candidate, 5 per section.

**Auto-selection is impossible by construction.** The extractor emits
`DiagnosisMeta.proposals` only; `ReportSection.icd10` is written solely
by report-service on the clinician's confirm. Two AST-level tests
enforce it: nlp-service contains no assignment to `icd10`/`icd10_codes`
and never imports the report content model.

The ICD-10 lookup is this stage's only I/O: finals-only, per-section,
bounded, and wrapped in a **50 ms timeout**. On timeout or error the
extractor emits nothing and the pipeline completes normally —
**fail-EMPTY**, deliberately unlike the sprint-11 consent gate, which
is correctly fail-CLOSED. That gate protects a patient right; this one
offers a convenience, and a hiccuping reference table must never fail a
clinician's dictation.
