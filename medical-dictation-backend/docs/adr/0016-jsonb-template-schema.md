# ADR-0016 — JSONB template schema + cosmetic-vs-structural rule

**Date:** 2026-06-23
**Status:** Accepted
**Deciders:** tech lead, clinical content lead, ML/MLOps lead

---

## Context

Sprint-06 introduces clinical templates — the structural definition of
what clinicians dictate (sections, prompts, voice aliases, billing
metadata). Sprint-8 reports persist `template_id` + `schema_version`
at finalization; sprint-11 encounters reference reports; sprint-17 FHIR
emits Composition.section structure from template metadata.

The schema design space:

| Approach          | Schema-change cost | Authoring workflow | Query patterns | Notes |
| ----------------- | ------------------ | ------------------ | -------------- | ----- |
| Relational (one table per field type) | high (migrations) | clinical content team needs DBA help | strong | rigid |
| Star schema (template + sections tables) | medium | OK | strong | every section is a row → 16 templates × 5-8 sections = 80-128 rows; admin UI is harder |
| JSONB (one document per template version) | low (Pydantic at boundaries) | YAML/JSON authoring | weaker (need ->>) | fast iteration |
| Document store (Mongo) | low | OK | weak | adds infra |

## Decision

**JSONB** for the template definition. The schema is validated by
`libs/template_models.TemplateDefinition` (Pydantic, `extra="forbid"`)
at every boundary that touches it (HTTP POST/PUT, seed loader, CI gate).

Cosmetic edits update the row in place and bump `schema_version`.
Structural edits insert a new row with `parent_template_id` set and
`schema_version = 1`.

**Cosmetic** = name, voice_aliases, asr_prompt, order, default_content,
metadata (non-FHIR fields).

**Structural** = section added/removed, `field_type` changed, `required`
flipped, `min_chars` increased.

## Why this distinction matters

Sprint-8 reports reference templates by ID. If a clinical content edit
removes a section that 1,000 existing reports populated, those reports
must remain readable as-they-were. Forcing structural changes to create
a new row preserves the lineage; existing reports point at the old row.

If we'd let a cosmetic edit silently change the section list, sprint-8
reports would suddenly have an "orphan section" (data without a
home in the template) and a missing section (template field with no
data). Clinical content leads would discover this only when a
clinician notices their report layout broke.

## Consequences

- **Authoring workflow**: clinical content lead edits a JSON file, runs
  `make seed-templates`, the seed function `upsert_system_template`
  takes care of either cosmetic update (in place, version bump) or
  structural new-row. The 16 sprint-06 templates pass validation in
  CI on every PR.
- **Query patterns**: list endpoint reads metadata columns only (fast).
  Detail endpoint reads JSONB (slower; cached in-process for 60 s).
- **Sprint-8 reports** persist both `template_id` and
  `template_version`; the diff endpoint surfaces template-shape
  differences as `metadata_changes`. The FK is `ON DELETE RESTRICT`
  (templates are soft-deleted, never hard-deleted).
- **Sprint-17 FHIR**: emits Composition.section structure from
  template.metadata.fhir_template; cosmetic edits don't break the
  mapping; structural edits emit a new Composition profile linked to
  the new template version.

## Alternatives considered

- **Star schema (template + sections tables)**: rejected — admin UI
  to edit a section becomes "edit a row in another table", which is
  harder to wire than a JSON form. Versioning is also harder: a
  structural change requires creating new rows in both tables.
- **Document store**: rejected — adds a new dependency for a small
  amount of data (16 templates per tenant × ~30 tenants = 500 rows
  is well within Postgres + JSONB scale).

## Migration path off JSONB

If sprint-17+ admin UI demands richer query patterns (e.g., "show me
every template that mentions BP normalization"), we can introduce
materialized views over JSONB. If that's not enough, we re-shape into
a star schema with a one-shot migration; the Pydantic boundary stays
the same.

## Trigger conditions for revisiting

- JSONB query patterns becoming a bottleneck (unlikely; even at
  10× current scale, JSONB + GIN is fine).
- Clinical content team wants in-app authoring (sprint-17 nice-to-have);
  doesn't change the schema decision.

---

## Amendment (2026-06-23) — `SwitchSection` is v1-additive

The `medical-dictation.v1` protocol gains a `SwitchSection` client
message. Sprint-04 ADR-0012 reserved subprotocol version bumps for
breaking changes; `SwitchSection` is **additive**:

- v1 clients that never send `SwitchSection` are unaffected.
- v1 servers accept the new message because the discriminated union
  was extended in-place (Pydantic's discriminator routes to the new
  type when `type == "switch_section"`; otherwise the union behaves
  exactly as before).
- `extra="forbid"` on every message means a future v2-additive field
  on `SwitchSection` would be rejected by v1 servers — which is the
  correct semantics: a v2 client must connect with `medical-dictation.v2`
  to use v2-additive fields.

No subprotocol bump. Documented in `docs/api/dictation-ws-v1.md`.
