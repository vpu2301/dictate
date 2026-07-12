# Autocomplete corpus — authoring contract

Migration `0026_seed_autocomplete_system_corpus.sql` seeds the starter
corpus (30 phrases + 7 snippets across UK + EN). The committed files in
this directory are its **single source of truth** — a unit test
(`test_corpus_contract.py::test_emit_sql_matches_migration_0026`) fails
if the SQL and these files drift apart.

The production corpus (**~10k UK / ~3k EN phrases, ~60 snippets**) is a
**clinical-content-lead deliverable** (named carry-over in
`docs/sprint-10/SPRINT-TODO.md` and `todo.md`). Engineering does not
author clinical content beyond the starter set: unreviewed clinical
content is a patient-safety and credibility risk.

## File formats

`phrases_<language>.csv` — UTF-8, header row required:

```csv
phrase,language,specialty,section_hint
задишка при фізичному навантаженні,uk,cardiology,anamnesis
тони серця ясні ритмічні,uk,cardiology,examination
цукровий діабет 2 типу,uk,endocrinology,diagnosis
скарг на момент огляду не пред’являє,uk,general,anamnesis
повторна консультація через 2 тижні,uk,cardiology,follow_up
```

```csv
phrase,language,specialty,section_hint
shortness of breath on exertion,en,cardiology,anamnesis
regular sinus rhythm,en,cardiology,examination
no acute distress,en,general,examination
continue beta-blocker therapy,en,cardiology,plan
follow up in two weeks,en,general,follow_up
```

`snippets_<language>.json` — JSON array:

```json
[
  {
    "trigger": "vitals",
    "expansion": "Температура {_} °C, АТ {_} мм рт ст, ЧСС {_} за хвилину.",
    "cursor_position": 13,
    "language": "uk"
  }
]
```

`{_}` marks fill-in slots; `cursor_position` is where the caret lands
after expansion (0 ≤ n ≤ expansion length). At request time the
clinician types the trigger with a leading slash (`/vitals`) — store
the trigger **without** the slash.

## Authoring rules

1. **Granularity** — complete clinical clauses a doctor actually types
   ("аускультативно дихання везикулярне"), not single words and not
   whole paragraphs (paragraphs belong in snippets).
2. **Register** — formal clinical Ukrainian (’ apostrophe, not ');
   standard clinical English.
3. **Limits** (validator + DB enforce): phrase ≤ 80 chars; trigger
   2–32 chars; expansion ≤ 4000 chars; no leading/trailing whitespace;
   languages `uk`/`en` only.
4. **Absolute PII prohibition** — no names, no identifiers, no numbers
   that could be one (10-digit IPN-like, 13-digit id-like, 7+-digit
   phone-like, dates, emails, passport patterns). The validator rejects
   them mechanically; generic clinical numbers like "АТ 120/80" are fine.
5. **Split by specialty** via the `specialty` column; `section_hint`
   maps to report sections (`anamnesis`, `examination`, `diagnosis`,
   `plan`, `follow_up`).
6. Target ≈10k UK phrases split by specialty; duplicates
   (case-insensitive per language) are rejected.

## Review flow

1. Author edits the CSV/JSON files (any editor; UTF-8).
2. Validate locally — only Python + uv needed:

   ```bash
   uv run python scripts/validate-autocomplete-corpus.py my_phrases.csv
   ```

   A malformed or PII-containing file fails loudly with row-level
   messages; a clean run prints per-language counts and
   "ready to load".
3. Hand the validated files to engineering.
4. Engineering renders them into a migration:

   ```bash
   uv run python scripts/validate-autocomplete-corpus.py --emit-sql > new_seed.sql
   ```

5. Migration PR; **clinical lead signs off on the PR** before merge.

## CI gate

`make check-autocomplete-corpus` (part of `make ci` and the PR
pipeline) validates every committed file in this directory. The PII
pattern set mirrors the service scrubber
(`autocomplete_service/scrubber.py`); a unit test pins the two sets
together, so a scrubber change forces a validator change.
