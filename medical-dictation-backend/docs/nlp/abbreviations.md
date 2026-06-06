# Abbreviation Policy

Sprint-05 Stage 5 applies a per-tenant + global merged dictionary to
the post-processed text.

## Schema

```
abbreviation_dictionary(
    id, tenant_id NULL=global, language,
    expanded, abbreviated,
    direction (expand | compact | either),
    domain (cardiology / endocrinology / pulmonology / neurology / all / NULL),
    case_sensitive
)
```

Tenant rows override global on the same `(language, expanded, abbreviated)`.

## Direction

- `compact` — REPLACE expanded form with abbreviated. Default.
- `expand` — REPLACE abbreviated with expanded.
- `either` — pass through (the clinician's surface form wins).

## Snapshot semantics

The processor fetches the merged dictionary ONCE per request at
`POST /nlp/process` entry. Admin edits made during an in-flight
request DO NOT affect that request — the snapshot is immutable for
the request's duration.

The snapshot's `fingerprint` (a stable hash) is part of the
idempotence cache key. An admin edit invalidates cached results for
that tenant on the next request.

## Word-boundary matching

Substitutions respect Unicode word boundaries. `ІМ` will NOT match
inside `імпорт` (no boundary on the left). Case sensitivity is per-row.

## Domain filtering

If `ProcessingContext.specialty` is set (e.g., `cardiology`), rules
with `domain=specialty` win over `domain='all'` win over `domain=NULL`.

This is what lets `ІМ` mean "infarct" in a cardiology session and
remain ambiguous otherwise.

## Admin API

- `GET /nlp/abbreviations[?language=]` — paginated list of merged rules.
  Role: any authenticated.
- `PUT /nlp/abbreviations` — upsert one tenant rule. Body:
  `{language, expanded, abbreviated, direction, domain?, case_sensitive?}`.
  Role: `tenant_admin`. Emits `abbreviation.policy.set` audit.
- `DELETE /nlp/abbreviations/{id}` — remove one tenant rule (global
  rules are immutable). Role: `tenant_admin`. Emits
  `abbreviation.policy.deleted` audit.

## RLS

Reads see own-tenant rows OR global rows. Writes are limited to
own-tenant rows. Cross-tenant access is impossible at the DB layer
(sprint-02 RLS-first invariant).

## Global seed

`infra/postgres/seed/abbreviations_global.sql` ships ~40 cardiology /
endocrinology / pulmonology / neurology starter entries reviewed by
the clinical content lead. Tenants can override any of them via
`PUT /nlp/abbreviations`.
