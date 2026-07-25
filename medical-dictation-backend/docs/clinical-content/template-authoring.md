# Template Authoring Guide

For the clinical content lead + linguist consultant.

## Workflow

1. **Edit / create** a JSON file at `infra/seeds/templates/<code>.json`.
   The file name MUST match `code`.
2. **Validate**: `python scripts/validate-templates.py`.
3. **PR review**: clinical content lead + linguist sign off in PR
   description.
4. **Deploy**: merging the PR triggers a redeploy; `make seed-templates`
   pulls the JSON into the DB via `upsert_system_template()`.

## JSON shape (cardiology example)

```json
{
  "code": "cardiology_outpatient_uk",
  "name": "Кардіологічна консультація (амбулаторна)",
  "language": "uk",
  "specialty": "cardiology",
  "schema_version": 1,
  "sections": [
    {
      "id": "anamnesis",
      "name": "Анамнез",
      "voice_aliases": ["анамнез", "розділ анамнез"],
      "required": true,
      "field_type": "free_text",
      "asr_prompt": "<≤ 224-token prompt>",
      "synthesis_prompt": "<optional sprint-12 prose guidance>",
      "min_chars": 30,
      "order": 0
    }
  ],
  "metadata": {
    "moh_order_ref": "MoH-110-cardiology",
    "fhir_template": "DiagnosticReport/cardiology-consultation"
  }
}
```

### ASR prompt rules

- **≤ 224 tokens** (Whisper's `initial_prompt` window). The validator
  uses tiktoken to enforce.
- Prefer **medically representative vocabulary** the clinician
  actually uses — abbreviations, units, drug names, anatomy.
- Don't pad with generic terms (e.g., "клінічна консультація,
  лікування, діагностика"). Whisper biases away from anything not in
  the prompt; tight is better than padded.

### Voice alias rules

- **Lowercase** (validator lowercases automatically).
- **Unique across the template**: two sections can't claim the same
  alias. The validator rejects.
- **No overlap with common dictation vocabulary**: "діагноз" alone is
  a borderline alias because clinicians say it as content. The
  matcher (sprint-05) requires pause-before + min-probability to
  reduce false positives, but a distinctive prefix like "розділ
  діагноз" is safer.

### `field_type` choices (sprint-06)

| Value                    | Use case                       |
| ------------------------ | ------------------------------ |
| `free_text` (default)    | most narrative sections        |
| `structured_diagnosis`   | the "Diagnosis" section (sprint-13 anamnesis may render ICD-10 picker) |
| `date`                   | calendar-date input            |
| `date_with_note`         | date + short free-text         |
| `numeric_with_unit`      | BP, HR, lab values             |
| `choice` (sprint-13)     | single-select — smoking status, pregnancy status |
| `multi_choice` (sprint-13) | multi-select — allergies, risk factors |

New field types require a Pydantic model bump and a corresponding
frontend renderer.

### `options` (sprint-13 — `choice` / `multi_choice` only)

A `choice`/`multi_choice` section MUST carry 2–50 `options`; every
other field type must carry none (the validator rejects both
violations). Each option:

```json
{
  "value": "never",
  "label": "Не палить",
  "voice_aliases": ["не палить", "не курить", "ніколи не палив"]
}
```

- **`value`** — lower-case slug, ≤ 64 chars, the *stable identity*
  persisted in report content. Renaming or removing a `value` is a
  **structural** edit (stored selections would dangle). Pick it once,
  pick it well.
- **`label`** — what the frontend renders (1–128 chars). Labels must
  be unique per section case-insensitively. Label-only changes are
  cosmetic.
- **`voice_aliases`** — the sprint-13 extractor's fuel: the phrases a
  clinician actually *says*. Normalized at validation (NFC, lower,
  stripped, ≤ 64 chars each); must be unique across the section's
  options (an ambiguous alias would force the extractor to guess —
  it never does). Be generous: include gendered verb forms
  ("кинув/кинула палити"), synonyms (палити/курити), and clinical
  shorthand. Adding an alias is cosmetic.
- **No PII, ever.** Options are shared clinical vocabulary; the
  validator sweeps labels and aliases with the same PII patterns as
  the autocomplete corpus (phone, ІПН, email, passport, DOB-like,
  med-ID) and fails the CI gate on a hit.

The extractor only proposes an option when a normalized alias/label
matches above threshold; otherwise the field stays empty and the
dictated prose is preserved. Alias quality directly drives extraction
recall — review aliases whenever the pilot shows misses.

### Compound measurements (`numeric_with_unit` limitation, sprint 13)

A `numeric_with_unit` section holds **one** value and **one** unit.
Blood pressure is dictated as a pair and normalizes to `140/90`, which
is not a single value — so a BP section extracts **nothing** and the
clinician fills it manually.

This is deliberate. Special-casing "/" to invent a single number would
be a clinical error dressed as a convenience. If a template needs BP as
typed data today, model it as **two** numeric sections (systolic and
diastolic); a proper compound field type is future work. The dictated
prose is always preserved either way.

The same applies to any paired measurement (e.g. visual acuity
"0,8 / 0,9").

### `synthesis_prompt` (optional)

Per-section guidance read by **sprint-12 (Gemma)** to turn the dictated
raw text into the section's final prose — e.g. "write in the third
person, preserve doses verbatim, don't invent ICD-10 codes". It does
**not** affect ASR. Optional: an empty value means "no section-specific
synthesis guidance". Editing it is a **cosmetic** change (no new row).
Max 2 000 chars.

### `min_chars` semantics

The minimum character count for the section to be considered "filled"
by sprint-8's finalize validation. Setting `min_chars=30` says: a
report can be saved as a draft with this section shorter, but it
cannot be **finalized**.

### Metadata

- `moh_order_ref`: free-text reference to MoH Order 110 alignment
  (Ukrainian regulatory).
- `billing_code`: free-text internal billing ID; sprint-17 admin may
  map.
- `fhir_template`: hint for sprint-17 FHIR Composition emission.
  Format: `<ResourceType>/<canonical-id>`.

## Cosmetic vs structural edits

See ADR-0016. **Structural edits create a new row**, leaving the old
row intact for existing reports to reference. The classifier is
deterministic; the PUT response carries the `kind`.

### Cosmetic edit (in-place + version bump)

- Change a section's `asr_prompt` to fix a typo: ✓ cosmetic.
- Rename a section: ✓ cosmetic.
- Add a voice alias: ✓ cosmetic.
- Add an option to a choice section: ✓ cosmetic (sprint-13).
- Change an option's `label` or add/change its aliases: ✓ cosmetic.

### Structural edit (new row)

- Add a new section: structural (existing reports won't have data
  for it).
- Remove a section: structural (existing reports' data goes
  orphan-less).
- Flip `required: false → true`: structural (validation gets
  tighter; existing draft reports may now fail finalize).
- Remove or rename an option `value` (sprint-13): structural
  (reports that stored the old value would reference a
  no-longer-existing option).

## Pilot week checklist

- [ ] Real clinician dictates against each template (one session each
  for all system templates).
- [ ] Linguist reviews any voice aliases that triggered or didn't
  trigger.
- [ ] Clinical content lead checks each ASR prompt against the
  resulting transcript — is the medical vocabulary surfacing?
- [ ] Sample 5 sessions per template; eyeball WER against gold.
