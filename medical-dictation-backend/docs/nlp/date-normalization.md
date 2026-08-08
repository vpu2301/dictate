# Date & Time Normalization

Sprint-05 Stage 4 transforms spelled / relative / colloquial dates
into a canonical form per the tenant's `date_format`.

## Format options

| `date_format`    | Output for May 1, 2026   |
| ---------------- | ------------------------- |
| `DD.MM.YYYY`     | `01.05.2026`              |
| `YYYY-MM-DD`     | `2026-05-01`              |
| `WORD`           | `1 травня 2026` / `May 1, 2026` / `1. Mai 2026` |

UK and DE tenants default to `DD.MM.YYYY`; EN tenants to `YYYY-MM-DD`.

## Relative dates

Anchored to `ProcessingContext.reference_date` (the caller passes it;
defaults to server `now()` with a `missing_reference_date` warning).

| Ukrainian          | English         | German                        | Offset             |
| ------------------ | --------------- | ----------------------------- | ------------------ |
| `сьогодні`         | `today`         | `heute`                       | +0 days            |
| `вчора` / `учора`  | `yesterday`     | `gestern`                     | −1 day             |
| `позавчора`        | —               | `vorgestern`                  | −2 days            |
| `завтра`           | `tomorrow`      | `morgen`                      | +1 day             |
| `післязавтра`      | —               | `übermorgen`                  | +2 days            |
| `наступного тижня` | `next week`     | `nächste/kommende Woche`      | +7 days            |
| `минулого тижня`   | `last week`     | `letzte/vergangene Woche`     | −7 days            |
| `у <weekday>`      | `on <weekday>`  | `am <Wochentag>`              | next future weekday|

**`morgen` is deliberately context-gated.** The same word is both
*tomorrow* and *morning*, and capitalization can't settle it (the
punctuation model is not reliable enough to bet a date on). `am
Morgen`, `guten Morgen`, `jeden/diesen Morgen` and `morgen früh /
abend / mittag / nachmittag` are left as text; everything else resolves
to a date.

## Absolute dates

- Numeric: `01.05.2026` / `2026-05-01`.
- Word-form UK: `1 травня 2026` (declined month).
- Spelled ordinal day UK: `третього травня` → `03.05.2026`, including
  compounds (`двадцять першого грудня` → `21.12.<year>`). Clinicians
  dictate the day as a genitive ordinal, which Stage 3 (cardinals only)
  leaves untouched, so Stage 4 maps `першого…тридцять першого` directly.
- Word-form EN: `May 1, 2026` or `May first 2026`.
- German: `5. März 2026`, `5 März`, and the spelled ordinal
  `am fünften März` → `05.03.<year>` (all four case endings, up to
  `einunddreißigsten`). `Jänner` is accepted for January.

Year defaults to `reference_date.year` if omitted.

## Ambiguous dates

A date that fails Python's `date()` constructor (e.g., `31.04.2026` —
April has 30 days) is NOT corrected. It passes through as
`31.04.2026` and emits `Warning{code="ambiguous_date"}`. Sprint 08's
clinical-rules layer validates and surfaces to the clinician.

## Times

- `о пів на <hour>` / `half past <hour>` → `HH:30`.
- German `um 8 Uhr [30]` → `08:00` / `08:30`. `Uhr` is required — a bare
  "um 8" is as often a dose interval as a clock time.
- German `halb acht` → **`07:30`**, not 08:30: `halb` counts down to the
  named hour. `Viertel vor/nach zehn` → `09:45` / `10:15`. `halb eins`
  is `12:30` — a spoken 12-hour clock names the coming hour and midday
  is the overwhelmingly likely reading in a consultation.
- Explicit `HH:MM` passes through.

## Reference-date discipline

`reference_date` is a CLIENT responsibility. If omitted, the server
fills it from `now()` AND embeds the resolved date in the idempotence
cache key — so cached re-runs are deterministic even when the caller
didn't pin the date.

A `missing_reference_date` warning fires every time the server falls
back; pilot week catches callers that aren't pinning their reference.
