# Voice Commands (medical-dictation.v1)

The voice command FSM (sprint 05 Stage 1) detects intentional verbal
commands embedded in dictation. Every match has three gates:

1. **Pause-before**: silence ≥ `requires_pause_before_ms` between the
   previous content word and the command head.
2. **Confidence**: average word probability ≥ `min_avg_probability`.
3. **Edit-distance tolerance**: ≤ 1 substitution per phrase;
   substituted word's Levenshtein distance ≤ 2.

False positives cost trust more than false negatives — defaults err
toward "didn't fire". Frontend offers a 600-ms undo affordance and
emits `voice_command.undone` for telemetry.

## Catalogue

### Ukrainian (15 intents)

| Intent              | Canonical phrases                                | Pause | Conf | Notes |
| ------------------- | ------------------------------------------------ | ----- | ---- | ----- |
| `newparagraph`      | новий абзац, абзац, новий параграф               | 250   | 0.85 | Paragraph break |
| `newline`           | новий рядок, перенос рядка                       | 200   | 0.85 | Line break |
| `period`            | крапка, крапку                                   | 300   | 0.88 | Anti-idiom guard ("крапка над і") |
| `comma`             | кома, кому                                       | 250   | 0.88 |       |
| `question_mark`     | знак питання, питальний знак                     | 250   | 0.85 |       |
| `section.diagnosis` | розділ діагноз, перейти до діагнозу              | 350   | 0.85 | Section ID resolved by template |
| `section.history`   | розділ анамнез, перейти до анамнезу              | 350   | 0.85 |       |
| `section.exam`      | розділ огляд, об'єктивний огляд                  | 350   | 0.85 |       |
| `section.plan`      | розділ план, план лікування                      | 350   | 0.85 |       |
| `insert_template`   | вставити шаблон, шаблон                          | 300   | 0.85 | Sprint 06 wires args |
| `save_draft`        | зберегти чернетку, зберегти як чернетку          | 300   | 0.85 |       |
| `undo_last`         | відмінити останнє, скасувати останнє             | 300   | 0.85 |       |
| `stop_dictation`    | стоп диктування, зупинити диктування             | 350   | 0.85 |       |
| `begin_quote`       | відкрити лапки, цитата початок                   | 250   | 0.85 |       |
| `end_quote`         | закрити лапки, цитата кінець                     | 250   | 0.85 |       |

### English (15 intents)

| Intent              | Canonical phrases                       | Pause | Conf |
| ------------------- | --------------------------------------- | ----- | ---- |
| `newparagraph`      | new paragraph, paragraph break          | 250   | 0.85 |
| `newline`           | new line, line break                    | 200   | 0.85 |
| `period`            | period, full stop                       | 300   | 0.88 |
| `comma`             | comma                                   | 250   | 0.88 |
| `question_mark`     | question mark                           | 250   | 0.85 |
| `section.diagnosis` | section diagnosis, go to diagnosis      | 350   | 0.85 |
| `section.history`   | section history, go to history          | 350   | 0.85 |
| `section.exam`      | section exam, physical exam             | 350   | 0.85 |
| `section.plan`      | section plan, treatment plan            | 350   | 0.85 |
| `insert_template`   | insert template, template               | 300   | 0.85 |
| `save_draft`        | save draft, save as draft               | 300   | 0.85 |
| `undo_last`         | undo last, undo that                    | 300   | 0.85 |
| `stop_dictation`    | stop dictation, end dictation           | 350   | 0.85 |
| `begin_quote`       | open quote, quote begin                 | 250   | 0.85 |
| `end_quote`         | close quote, quote end                  | 250   | 0.85 |

### German (15 intents)

The German catalogue mirrors the English intent set exactly (a test
pins the two sets together — a missing intent is a German session where
a command silently does nothing).

| Intent              | Canonical phrases                          | Pause | Conf |
| ------------------- | ------------------------------------------ | ----- | ---- |
| `newparagraph`      | neuer Absatz, Absatz, neuer Abschnitt      | 250   | 0.85 |
| `newline`           | neue Zeile, Zeilenumbruch                  | 200   | 0.85 |
| `period`            | Punkt                                      | 300   | 0.88 |
| `comma`             | Komma                                      | 250   | 0.88 |
| `question_mark`     | Fragezeichen                               | 250   | 0.85 |
| `section.diagnosis` | Abschnitt Diagnose, gehe zu Diagnose       | 250   | 0.85 |
| `section.history`   | Abschnitt Anamnese, gehe zu Anamnese       | 250   | 0.85 |
| `section.exam`      | Abschnitt Untersuchung, körperliche Untersuchung | 250 | 0.85 |
| `section.plan`      | Abschnitt Plan, Behandlungsplan            | 250   | 0.85 |
| `insert_template`   | Vorlage einfügen, Vorlage                  | 250   | 0.85 |
| `save_draft`        | Entwurf speichern, als Entwurf speichern   | 250   | 0.85 |
| `undo_last`         | letztes rückgängig, rückgängig machen      | 250   | 0.85 |
| `stop_dictation`    | Diktat beenden, Diktat stoppen             | 250   | 0.85 |
| `begin_quote`       | Zitat Anfang, Zitat Beginn                 | 250   | 0.85 |
| `end_quote`         | Zitat Ende                                 | 250   | 0.85 |

**German-specific hardening.** Two families are `exact_match_only`,
found by the German TP/FP corpus, not by review:

- **`auf` vs `zu`** — "Klammer auf" and "Klammer zu" are ONE edit apart,
  so the FSM's 1-substitution tolerance fired the opposite bracket.
  Every open/close pair (parens, brackets, braces, quotes) is exact.
- **Short single-word heads** — "Punkt", "Komma", "Doppelpunkt",
  "Bindestrich", "Schrägstrich", "Raute", plus the ellipsis phrase
  "drei Punkte". German inflection puts ordinary prose one edit away
  ("die Punkte sind gerötet", "Patient im Koma").

## Adding a new command

1. Edit `infra/postgres/seed/voice_commands_<lang>.json` — add a phrase
   list, pause threshold, confidence threshold, optional
   `is_section_command`.
2. Run `make seed-voice-commands` (the JSON fixtures are
   AUTHORITATIVE — the seeder deletes every row for a language before
   re-inserting, so a migration-only seed would be wiped; migration
   0055 seeds the same rows idempotently for migration-only
   environments, and a test pins the two sources together).
3. Add a test in `services/nlp-service/tests/unit/test_voice_command_matcher.py`
   covering the canonical phrase + at least one negative case.
4. Add a row to this catalogue.
5. If the command has a frontend operation, register it in
   `services/nlp-service/src/nlp_service/stages/operations.py`.

## Anamnesis commands (sprint 13)

Hands-free structured input: one utterance, zero taps. The option name
resolves against the **template's** `choice`/`multi_choice` sections,
and the operation carries the option **slug** — never the spoken words.

| Intent | uk | en | de | Operation | arg |
| --- | --- | --- | --- | --- | --- |
| `choice.set` | обрати / вибрати / встановити `<опція>` | select / choose / set `<option>` | auswählen / wählen / setzen `<Option>` | `set_choice` | `{section_key, value}` |
| `choice.add` | додати `<опція>` | add `<option>` | hinzufügen `<Option>` | `add_choice` | `{section_key, value}` |
| `choice.remove` | прибрати / видалити `<опція>` | remove / delete `<option>` | entfernen / löschen `<Option>` | `remove_choice` | `{section_key, value}` |
| `diagnosis.capture` | діагноз / основний діагноз | diagnosis / primary diagnosis | Diagnose / Hauptdiagnose | `mark_diagnosis_text` | `{from_word_index}` |

### Rules that make this safe

- **Exact option matching only.** The FSM never fuzzy-matches an option
  name. Fuzziness belongs in the extractor, where a wrong guess is a
  proposal the clinician can reject; a voice command **writes**, so a
  near-miss must never become a selection.
- **Exact heads for these specs** (`exact_match_only`). "прибрати"
  (remove) is Levenshtein-2 from "обрати" (set) — with the FSM's normal
  1-substitution tolerance, "remove penicillin" would have selected it
  instead. Opposite clinical statements must not be one typo apart.
- **A voice selection is `manual`, not `extracted`.** The clinician said
  it explicitly; nothing was inferred. The FE writes
  `field_specific_metadata.source = "manual"` (and therefore omits
  `confidence` — see the metadata contract).
- **Strict field semantics.** `add`/`remove` on a single-select section
  no-op with `reason: "not_a_multi_choice_section"` rather than being
  reinterpreted as `set`. `set` on a multi-select **replaces the whole
  selection** — a judgment call for predictability, cheap to revisit
  with pilot feedback.
- **An unrecognised option name is prose, not a no-op.** A no-op would
  still consume the command head, deleting a word from the note —
  "встановити діагноз поки неможливо" must stay untouched. A `reason`
  no-op is emitted only when an option was positively recognised
  (`option_ambiguous`, `not_a_multi_choice_section`), so the FE can
  toast precisely.
- **No ICD-10 by voice.** `diagnosis.capture` only marks where dictated
  diagnosis text begins; code selection stays a confirm action
  (sprint-13 scope).

Commands are stripped from the text by stage 1, so the sprint-13
extractor never sees a command phrase — one utterance cannot both
select via voice and extract from the same words (test-enforced).

## Known false-positive sources (pilot week)

- **"слід" → `slash`** — FIXED in sprint 13. "слід" is Levenshtein-2
  from "слеш", and "слід призначити…" / "слід виключити…" are extremely
  common in Ukrainian clinical prose, so the fuzzy match was inserting a
  stray "/" into notes. The `slash` spec is now `exact_match_only`.
  Found by the sprint-13 TP/FP corpus
  (`tests/fixtures/command_corpus_uk.py`).

- **"Klammer zu" → `open_paren`** — FIXED when German was added. See
  the German hardening note above; found by
  `tests/fixtures/command_corpus_de.py`.

- **"крапка над і"** — Ukrainian idiom; the pause-before gate (300 ms)
  catches the mid-phrase case. Verified day-9.
- **"розділ"** as a content word — guarded by the section argument
  resolution: if no matching template section, the command is rejected
  and the word becomes content.

## Frontend hand-off

Every `Final` message carries `voice_command: null` (sprint 05 default)
or a populated slot. The `operations` array always carries the
frontend-actionable derivative (see `operations.py`).
