# Runbook — structured anamnesis & field extraction

Sprint 13. Stage: `nlp_service/stages/field_extraction.py` (ADR-0032).
Dashboard: **Sprint 13 — Structured Anamnesis & Extraction Quality**
(`sprint-13-extraction`). Alerts: `infra/prometheus/rules/sprint-13-alerts.yml`.

## The one thing to understand first

The extractor **proposes; the clinician confirms.** Below threshold,
ambiguous, negated, or unresolvable ⇒ the field stays **empty** and the
dictated prose is preserved. Nothing here ever guesses, and no ICD-10
code is ever auto-selected.

So "the field didn't fill" is usually **correct behaviour**, not an
incident. The question worth asking is the opposite one: *when it did
fill, was it right?* That is the override rate.

## Weekly ritual (with the clinical lead)

Open the dashboard next to the S10 acceptance panel. Together they
answer "is the structured layer helping or annoying?".

1. **Override rate by field_type** — the headline. Rising means
   clinicians are correcting us.
2. **Extraction outcomes** — a high `empty` rate for a field clinicians
   then fill by hand is an **alias gap**, not an extractor bug.
3. **ICD-10 zero-result rate** — how often the picker has no answer.
   While only the 239-code fixture is loaded this is expected to be
   high; the panel quantifies the acquisition carry-over instead of
   arguing about it.
4. **Unresolved option commands** — each one is an authoring task with
   data attached.

> **Do not tune from dev-team self-testing.** Every number here is only
> meaningful with real clinician traffic. The dashboard exists so
> tuning *waits* for that data.

## <a id="alert-override-rate"></a>Alert: FieldExtractionOverrideRateHigh

> Override rate > 30% for a field_type over 24 h (≥ 5 decisions).

Clinicians are replacing more than three in ten extracted values for
that field type. Nothing is broken — the safety rules held, a human
caught it — but the extractor is costing more attention than it saves.

**Triage, in order:**

1. **Alias coverage first.** Recall is bounded by aliases *by design*
   (the extractor never compensates by matching more loosely). Pull the
   overridden values from the audit log:

   ```sql
   SELECT payload_jcs->'payload' AS p
   FROM audit.events
   WHERE kind = 'anamnesis.field.overridden'
     AND created_at > now() - interval '7 days';
   ```

   The payload carries `was` (what we proposed) and `selected` (what
   the clinician chose) as **slugs** — never prose. If one pair
   dominates, the losing option's `voice_aliases` are too thin. Adding
   an alias is a **cosmetic** template edit (no new row) — see
   `docs/clinical-content/template-authoring.md`.
2. **Then the threshold.** `MDX_NLP_EXTRACTION_CONFIDENCE_THRESHOLD`
   (default 0.8). Raising it trades recall for precision. Treat a
   change like a pipeline change: it changes extraction output, so
   note it against `pipeline_version` (ADR-0032).
3. **Never "fix" it by loosening the safety rules.** The negation
   guard, the short-token rule, and the ambiguity rule are why a wrong
   value is rare. Loosening them to raise fill rate trades a visible
   annoyance for an invisible clinical error.

## <a id="alert-extraction-failures"></a>Alert: FieldExtractionFailuresElevated

> `no_lookup` / `unsupported` outcomes > 0.1/s for 15 m.

The extractor **fails empty by design**: an ICD-10 lookup timeout or a
missing table costs a proposal *silently* — the clinician sees no
error, only an unfilled field. This alert is that silence's only
visibility.

**Check, in order:**

1. Is `icd10_codes` populated? `SELECT count(*) FROM icd10_codes;`
   (0 ⇒ run `make seed-icd10-fixture`; see `docs/runbooks/icd10.md`).
2. Is the DB healthy? The lookup has a **50 ms timeout** against a
   measured p95 of ~1.7 ms, so timeouts mean the database is unwell,
   not that the budget is tight.
3. `unsupported` means a template carries a `field_type` the stage
   doesn't handle — a template/model version skew. Check which types
   the alert's series carries.

Fail-empty is deliberate here and deliberately *unlike* the sprint-11
consent gate, which is fail-**closed**. That gate protects a patient
right; this one offers a convenience, and a hiccuping reference table
must never fail a clinician's dictation.

## Tuning the threshold safely

```bash
# Observe first — at least a week of real traffic.
# Then, in staging:
MDX_NLP_EXTRACTION_CONFIDENCE_THRESHOLD=0.85 make run-nlp-service
```

Re-run the extraction corpus (`services/nlp-service/tests/fixtures/
extraction_corpus_uk.py`) at the new value: cases labelled `None` MUST
still yield nothing. A threshold change that starts filling a
previously-empty safety case is a regression, not an improvement.

## Voice-command friction

`option_ambiguous` means one spoken phrase names options in two
sections — fix by renaming an option label or narrowing aliases.
`not_a_multi_choice_section` means a clinician said "додати" on a
single-select field; if it recurs, the template's field type may be
wrong for how clinicians actually think about it.

An option name we simply don't recognise does **not** appear here: it
stays prose by design, because a no-op would have consumed the command
head and deleted a word from the note.

## Related

- ADR-0032 — stage order, matching rules, delivery path.
- `docs/runbooks/icd10.md` — reference table load + reload policy.
- `docs/architecture/reports.md` — metadata contract, typed-finalize
  rules, the confirmation flag.
- `docs/nlp/voice-commands.md` — the anamnesis command reference.
