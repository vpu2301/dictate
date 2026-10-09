# WER Eval Corpus — v2 (dev set)

The expansion corpus-v2 §2–§3 specifies: **86 replicas, 30 uk + 56 en**,
authored to be recorded into the **dev** half of the corpus.

`corpus-v2-replicas.csv` is the whole deliverable. It is the §6 import
format — UTF-8 with BOM, comma-separated — and imports through
`POST /corpus/eval/import` (or the console's «Імпорт CSV») with no manual
edits.

## Why this file exists at all

v1 is 34 lines, and the reason it stayed at 34 lines is that growing it
meant editing a Python module. EN was 4 utterances — a 12.9% WER on four
lines is a number with a ±30-point interval, which is to say not a number.
The CSV is the interface that makes the corpus a linguist's artefact rather
than a developer's.

## Columns

| Column | Values | Notes |
| --- | --- | --- |
| `id` | `{lang}-{category}-{NNN}` | unique system-wide; becomes a directory name in the exported corpus |
| `lang` | `uk` \| `en` \| `de` | German lines may be recorded but batch ASR cannot score them yet |
| `category` | `base` \| `numbers` \| `drugs` \| `abbrev` \| `codesw` \| `commands` | maps to the six adversarial subsets; `base` means subset NULL |
| `condition` | `headset` \| `phone_noise` | `phone_noise` → `phone-speaker-distance` in the schema |
| `set` | `dev` \| `test` | `test` is refused without an explicit confirmation (holdout guard) |
| `text` | the utterance | non-empty, ≤ 200 characters, quoted if it contains commas |
| `specialty` | *optional* | defaults to `general` |
| `transcript` | *optional* | gold text when it differs from the spoken form ("140/90" for "сто сорок на дев'яносто") |

## Coverage in this file

| Category | uk | en |
| --- | --- | --- |
| base | 2 | 10 |
| numbers, doses, units | 8 | 12 |
| drug names | 6 | 10 |
| abbreviations | 5 | 8 |
| code switching | 6 | 8 |
| voice commands | 3 | 8 |
| **total** | **30** | **56** |

27 of the 86 are `phone_noise` — the harsher recording condition, which §1.2
wants at ≥25% of the corpus so the clinic's worst case stays measurable
instead of being averaged away by headset recordings.

These counts are what bring each language to the ≥60 utterances §1.2 asks
for once the existing recordings are counted, which is the sample size at
which the overall bootstrap CI narrows to roughly ±4–5 points.

## Recording

Import first, record second: the recorder can only capture lines that exist
server-side (the scripted-only privacy invariant). Record headset conditions
first, then the phone/noise subset — a line with no take is simply absent
from a snapshot, so a partially recorded import still produces a valid,
smaller measurement.

Every attempt is journalled (`GET /corpus/eval/attempts`), including the ones
thrown away, so "this line cost six takes" is answerable afterwards.
