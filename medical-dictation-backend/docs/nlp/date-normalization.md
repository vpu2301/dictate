# Date & Time Normalization

Sprint-05 Stage 4 transforms spelled / relative / colloquial dates
into a canonical form per the tenant's `date_format`.

## Format options

| `date_format`    | Output for May 1, 2026   |
| ---------------- | ------------------------- |
| `DD.MM.YYYY`     | `01.05.2026`              |
| `YYYY-MM-DD`     | `2026-05-01`              |
| `WORD`           | `1 травня 2026` / `May 1, 2026` |

UK tenants default to `DD.MM.YYYY`; EN tenants to `YYYY-MM-DD`.

## Relative dates

Anchored to `ProcessingContext.reference_date` (the caller passes it;
defaults to server `now()` with a `missing_reference_date` warning).

| Ukrainian          | English         | Offset             |
| ------------------ | --------------- | ------------------ |
| `сьогодні`         | `today`         | +0 days            |
| `вчора` / `учора`  | `yesterday`     | −1 day             |
| `позавчора`        | —               | −2 days            |
| `завтра`           | `tomorrow`      | +1 day             |
| `післязавтра`      | —               | +2 days            |
| `наступного тижня` | `next week`     | +7 days            |
| `минулого тижня`   | `last week`     | −7 days            |
| `у <weekday>`      | `on <weekday>`  | next future weekday|

## Absolute dates

- Numeric: `01.05.2026` / `2026-05-01`.
- Word-form UK: `1 травня 2026` (declined month).
- Spelled ordinal day UK: `третього травня` → `03.05.2026`, including
  compounds (`двадцять першого грудня` → `21.12.<year>`). Clinicians
  dictate the day as a genitive ordinal, which Stage 3 (cardinals only)
  leaves untouched, so Stage 4 maps `першого…тридцять першого` directly.
- Word-form EN: `May 1, 2026` or `May first 2026`.

Year defaults to `reference_date.year` if omitted.

## Ambiguous dates

A date that fails Python's `date()` constructor (e.g., `31.04.2026` —
April has 30 days) is NOT corrected. It passes through as
`31.04.2026` and emits `Warning{code="ambiguous_date"}`. Sprint 08's
clinical-rules layer validates and surfaces to the clinician.

## Times

- `о пів на <hour>` / `half past <hour>` → `HH:30`.
- Explicit `HH:MM` passes through.

## Reference-date discipline

`reference_date` is a CLIENT responsibility. If omitted, the server
fills it from `now()` AND embeds the resolved date in the idempotence
cache key — so cached re-runs are deterministic even when the caller
didn't pin the date.

A `missing_reference_date` warning fires every time the server falls
back; pilot week catches callers that aren't pinning their reference.
