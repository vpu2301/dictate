# Audit kinds — Sprint 08 additions

Appends to the canonical catalogue in `docs/audit/audit-kinds.md`.

| kind                                | emitter            | payload keys                              |
| ----------------------------------- | ------------------ | ----------------------------------------- |
| `report.created`                    | report-service     | `code`, `version_id`                      |
| `report.draft.updated`              | report-service     | **aggregated**: `dictation_session_id`, `autosave_count`, `start_at`, `end_at`, `final_version_number` |
| `report.finalized`                  | report-service     | `version_number`                          |
| `report.reverted`                   | report-service     | (none)                                    |
| `report.cancelled`                  | report-service     | `reason`                                  |
| `report.amendment_drafted`          | report-service     | `version_number`, `amendment_type`, `parent_version_id` |
| `report.amended`                    | sprint-09 signing  | filled by signing path                    |
| `report.viewed_full`                | report-service     | `purpose` ∈ {clinical_continuity, audit, legal, qa_review, consultation, author}, `is_author` |
| `report.searched`                   | report-service     | `q`, `has_q`, `result_count`, `filters`   |
| `report.chain_integrity_failure`    | chain reconciler   | `anomaly_kind`, anomaly-specific detail   |

## Notes

- `report.draft.updated` is **aggregated per dictation session** (or
  flushed every 10 min). Not one event per PUT. This avoids audit
  volume blow-up during long dictation sessions.
- `report.viewed_full.purpose` is the read-purpose enum captured via
  `?purpose=` or `X-Read-Purpose`. Authors get `purpose=author` by
  default (no header required).
- `report.searched` records the *query string* and *result count*;
  individual results are NOT enumerated to keep the volume bounded.
  If DPO needs per-result trace, the search audit row + the user's
  subsequent `report.viewed_full` rows reconstruct the funnel.
- `report.chain_integrity_failure` is severity `sec` — pages security
  lead immediately.
