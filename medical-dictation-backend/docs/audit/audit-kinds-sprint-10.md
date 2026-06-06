# Audit kinds — Sprint 10 additions

Appends to `docs/audit/audit-kinds.md`. All kinds tenant-scoped, in
the sprint-02 hash-chained `audit.events`.

| kind                                          | emitter           | payload keys                          |
| --------------------------------------------- | ----------------- | ------------------------------------- |
| `autocomplete.phrase.created`                 | phrases router    | `source`, `language`                  |
| `autocomplete.phrase.updated`                 | phrases router    | `source`, fields_changed              |
| `autocomplete.phrase.deleted`                 | phrases router    | (none)                                |
| `autocomplete.phrase.write_rejected_pii`      | phrases router    | `patterns` (severity sec)             |
| `autocomplete.snippet.created`                | snippets router   | `source`, `trigger`                   |
| `autocomplete.snippet.updated`                | snippets router   | `trigger`                             |
| `autocomplete.snippet.deleted`                | snippets router   | `trigger`                             |
| `autocomplete.rollup.completed`               | rollup job        | `rollup_date`, `phrases_updated`      |
