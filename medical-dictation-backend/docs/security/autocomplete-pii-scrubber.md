# Autocomplete PII Scrubber Specification

## Purpose

Sprint-10 day-6 deliverable. The autocomplete service captures
telemetry that ranks phrases. Prefixes posted from the FE may
inadvertently contain partial PHI (patient names, IPN, DOB). This
scrubber redacts them before persistence.

DPO review of regex required before any sprint-10 ship and on every
regex change thereafter.

## Patterns redacted

| name      | regex                                              | replacement       | rationale                          |
| --------- | -------------------------------------------------- | ----------------- | ---------------------------------- |
| email     | `\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b`                 | `<redacted_PII>`  | obvious PII                        |
| ipn       | `\b\d{10}\b`                                       | `<redacted_PII>`  | Ukrainian IPN                      |
| med_id    | `\b\d{13}\b`                                       | `<redacted_PII>`  | 13-digit medical identifier        |
| passport  | `\b[A-Za-zА-ЯЇІЄҐа-яїієґ]{2}\s?\d{6}\b`            | `<redacted_PII>`  | UA passport format                 |
| dob_like  | `\b\d{1,2}[./-]\d{1,2}[./-]\d{4}\b`                | `<redacted_PII>`  | DOB-style dates                    |
| phone     | `(?<![\d+])\+?\d{7,14}(?!\d)`                      | `<redacted_PII>`  | phone-like sweep incl. int'l `+380…` |

## Behaviour

- Order-sensitive: more-specific patterns first (10-digit before
  the generic phone sweep).
- All matches replaced with the literal `<redacted_PII>` placeholder
  (no per-pattern variants — uniform downstream surface).
- Conservative bias: false positives (over-scrubbing) preferred over
  false negatives (PII leak).
- Known limitation: digit groups broken by spaces/dashes
  (`050 123 45 67`) are not caught — widening that far risks eating
  vitals sequences (`120 80`).

## Revision history

- **2026-07-07** — `phone` widened from `\b\d{7,9}\b` to
  `(?<![\d+])\+?\d{7,14}(?!\d)`: international `+380501234567`
  (12 digits) fell between the exact-10 `ipn` and exact-13 `med_id`
  patterns and leaked verbatim (observed live in the dev stack).
  **Pending DPO re-review** per the policy below.

## Call sites

1. **Telemetry intake** (`autocomplete_service.routers.telemetry`):
   prefix + context fields scrubbed before the buffer accepts the row.
2. **Phrase write** (`autocomplete_service.routers.phrases`):
   `contains_pii` is run on the user-supplied phrase; any match →
   422 + `autocomplete.phrase.write_rejected_pii` audit event.

## Test corpus

`services/autocomplete-service/tests/unit/test_scrubber.py` exercises
each pattern. Sprint-10 ships a 7-case test corpus inline; production
grows it via DPO + clinical content lead review.

## DPO sign-off log

| date       | reviewer | change                                | status   |
| ---------- | -------- | ------------------------------------- | -------- |
| pending    | DPO      | initial sprint-10 regex (this doc)    | pending  |

Any regex change requires a new row above + a re-review of the test
corpus.
