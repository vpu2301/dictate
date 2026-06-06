# Number & Unit Normalization

Sprint-05 Stage 3 transforms spelled-out and hybrid number expressions
into canonical short form. **Per-language rule-based** (ADR-0015).

## Coverage matrix

| Pattern              | UK example                                                | EN example                              | Output                       |
| -------------------- | --------------------------------------------------------- | --------------------------------------- | ---------------------------- |
| BP (systolic/diastolic) | `тиск сто двадцять на вісімдесят`                     | `blood pressure one twenty over eighty` | `тиск 120/80` / `BP 120/80`  |
| BP with units        | `… міліметрів ртутного стовпчика`                         | `… millimeters of mercury`              | `… мм рт. ст.` / `… mmHg`    |
| HR                   | `пульс сімдесят два за хвилину`                           | `pulse 72 bpm`                          | `пульс 72/хв` / `pulse 72 bpm` |
| Dose                 | `п'ять міліграм`                                          | `five milligrams`                       | `5 мг` / `5 mg`              |
| Decimal              | `сім цілих п'ять`                                         | `seven point five`                      | `7,5` (UK) / `7.5` (EN)      |
| Range                | `від ста до ста двадцяти`                                 | `from one hundred to one twenty`        | `100–120`                    |
| Time (half-past)     | `о пів на восьму`                                         | `half past seven`                       | `07:30`                      |
| Frequency            | `три рази на добу`                                        | `three times a day`                     | `3 разів/добу` / `3x/day`    |
| Generic NUM+UNIT     | `двадцять мілілітрів`                                     | `twenty milliliters`                    | `20 мл` / `20 ml`            |

## Per-tenant configuration

`tenants.settings` (sprint 17 admin UI surfaces these):

- `decimal_separator`: `","` (UK default) or `"."` (EN default).
- `bp_separator`: `"/"` default.
- `date_format` (Stage 4): `"DD.MM.YYYY"` / `"YYYY-MM-DD"` / `"WORD"`.

## Untagged numbers

A number with no surrounding unit/pattern marker is **passed through
unchanged**. "один пацієнт" stays "один пацієнт" — the parser refuses
to fold a single bare digit-word to "1" because the determiner is
clinically meaningful as text.

Spelled-out cardinals with ≥ 2 words DO fold even without a unit
nearby ("сто двадцять одна" → "121") because at that length the
parser's interpretation is unambiguous.

## English colloquial BP

"one twenty" (heard from a clinician saying BP 120) is normalized to
`120` by a colloquial-form heuristic: if the head is a single digit
(1–9) followed by a tens digit (10–90) with no `hundred`/`thousand` in
between, treat as `head*100 + tens`. The standard form
"one hundred twenty" still works through the regular parser.

## Known limitations

- Ukrainian genitive plural endings on units ("п'ять міліграмів")
  collapse to the canonical short ("5 мг") — case info is lost. Pilot
  session validated this is acceptable.
- Cross-language switching mid-text is not supported.
- Ordinals (Ukrainian declensions) have partial support for the common
  forms; the long tail is on the day-9 regression list.

## Latency budget

p95 ≤ 10 ms on a 50-word segment (rule-based; no model calls).
