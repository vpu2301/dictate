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

Sprint-13 adds `choice`, `multi_choice`. New field types require
a Pydantic model bump and a corresponding frontend renderer.

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

### Structural edit (new row)

- Add a new section: structural (existing reports won't have data
  for it).
- Remove a section: structural (existing reports' data goes
  orphan-less).
- Flip `required: false → true`: structural (validation gets
  tighter; existing draft reports may now fail finalize).

## Pilot week checklist

- [ ] Real clinician dictates against each template (one session each
  for all system templates).
- [ ] Linguist reviews any voice aliases that triggered or didn't
  trigger.
- [ ] Clinical content lead checks each ASR prompt against the
  resulting transcript — is the medical vocabulary surfacing?
- [ ] Sample 5 sessions per template; eyeball WER against gold.
