# Voice Commands (medical-dictation.v1)

> **Two layers handle spoken punctuation.** The prosody-gated FSM below
> (Stage 1) is the primary path for *streaming* dictation, where it can
> use word timing + probability and also emit frontend `operations`
> (undo, cursor moves). The deterministic **spoken-punctuation
> normalizer** (Stage 2b, see the section at the bottom) is a text-only
> safety net that catches command words the FSM misses — including the
> entire batch / text-only path, where there are no word timings and the
> FSM is inert.

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

## Adding a new command

1. Edit `infra/postgres/seed/voice_commands_<lang>.json` — add a phrase
   list, pause threshold, confidence threshold, optional
   `is_section_command`.
2. Run `python scripts/seed/seed_voice_commands.py --dsn <dsn>`.
3. Add a test in `services/nlp-service/tests/unit/test_voice_command_matcher.py`
   covering the canonical phrase + at least one negative case.
4. Add a row to this catalogue.
5. If the command has a frontend operation, register it in
   `services/nlp-service/src/nlp_service/stages/operations.py`.

## Known false-positive sources (pilot week)

- **"крапка над і"** — Ukrainian idiom; the pause-before gate (300 ms)
  catches the mid-phrase case. Verified day-9.
- **"розділ"** as a content word — guarded by the section argument
  resolution: if no matching template section, the command is rejected
  and the word becomes content.

## Frontend hand-off

Every `Final` message carries `voice_command: null` (sprint 05 default)
or a populated slot. The `operations` array always carries the
frontend-actionable derivative (see `operations.py`).

## Spoken-punctuation normalizer (Stage 2b — deterministic net)

`stages/spoken_punctuation.py` + `stages/spoken_punctuation_stage.py`
convert spoken punctuation **words** the ASR transcribed literally
(`крапка`, `кома`, `period`, `comma`, …) into the actual marks. It runs
right after the ML punctuation stage, on the **text alone** — no timing
or probability required — so it works on the batch/text path and rescues
any command the prosody FSM's conservative gates let through.

Why a second layer instead of loosening the FSM gates: the FSM must stay
conservative (a false "period" mid-sentence is worse than a missed one)
*and* it needs word timings it doesn't have on the batch path. A separate
text-level pass can afford deterministic, context-checked replacement.

### Command map

| Language | Phrase(s)                     | Result        |
| -------- | ----------------------------- | ------------- |
| uk       | крапка, крапку                | `.`           |
| uk       | кома                          | `,`           |
| uk       | двокрапка                     | `:`           |
| uk       | крапка з комою                | `;`           |
| uk       | знак питання                  | `?`           |
| uk       | знак оклику                   | `!`           |
| uk       | новий рядок / новий абзац     | `\n` / `\n\n` |
| uk       | питання (trailing only)       | `?`           |
| en       | period, full stop             | `.`           |
| en       | comma                         | `,`           |
| en       | colon                         | `:`           |
| en       | semicolon                     | `;`           |
| en       | question mark                 | `?`           |
| en       | exclamation mark / point      | `!`           |
| en       | new line / new paragraph      | `\n` / `\n\n` |
| en       | dot (non-numeric context)     | `.`           |

Longest phrase wins (`крапка з комою` beats `крапка`; `знак питання`
beats bare `питання`).

### Conservative guards (avoid corrupting prose)

Words that are also normal vocabulary are only converted in an
unambiguous context:

- **`dot`** — kept when flanked by a number word/digit (`three dot five`,
  `3 dot 5`) or a URL token (`example dot com`).
- **`period`** — kept before `of` (`a period of two weeks`).
- **`питання`** — converted only as the *trailing* token of a clause
  (`… нормально питання` → `…?`); mid-sentence it stays the noun
  "question". The unambiguous form `знак питання` always converts.

### Spacing / capitalization

No space before a mark, one space after; a terminator (`.`/`!`/`?`) or a
line break capitalizes the next word (`,`/`:`/`;` do not); line/paragraph
breaks never introduce duplicate spaces. A redundant mark an upstream
punctuator attached to the previous token is collapsed.

### Adding a command or language

Edit `_UK_RULES` / `_EN_RULES` (or add a new `_RULES_BY_LANGUAGE` entry)
in `stages/spoken_punctuation.py`, then add cases to
`tests/unit/test_spoken_punctuation.py` — including a regression case
proving normal prose with that surface word is left intact.
