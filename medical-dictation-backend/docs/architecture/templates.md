# Templates Architecture (Sprint 06)

## Where the slice lives

`services/report-service/` ships the templates slice. Sprint-08 will
extend it with reports. ADR-0016 explains why templates and reports
co-locate.

## JSONB schema (the contract)

`libs/template_models.TemplateDefinition` (Pydantic, `extra="forbid"`)
validates every JSON document on the way in. The five field types:
`free_text`, `structured_diagnosis`, `date`, `date_with_note`,
`numeric_with_unit`. Sprint-13 anamnesis adds more.

## Visibility model

Each `templates` row is either:

- **System** (`tenant_id IS NULL`, `is_system=true`) — managed by DBA
  migration; readable by every tenant.
- **Tenant** (`tenant_id IS NOT NULL`) — owned by one tenant; cloned
  from a system row (or another own row) via `POST /templates/clone`.

RLS enforces: tenants see own + system rows. Writes are restricted to
own-tenant rows. System rows can only be inserted via the
`tenant_writer` role (DBA migration).

## Versioning rule (ADR-0016)

`PUT /templates/{id}` diffs old vs new via `template_models.classify_edit`:

- **Cosmetic** → UPDATE in place + `schema_version` bump (name,
  voice_aliases, asr_prompt, order, default_content, metadata).
- **Structural** → INSERT new row with `parent_template_id` set +
  `schema_version = 1` (section added/removed, `field_type` changed,
  `required` flipped, `min_chars` increased).

Sprint-8 reports persist `template_id` + `template_version` at
finalization. Templates are **never hard-deleted** — soft-delete only.

## Section-aware dictation

```
clinician dictates voice command "розділ діагноз"
   │
   ▼
nlp-service (sprint-05) Stage 1 emits Operation:
   {op: "navigate_section", arg: {section_id: "<id>"}}
   │
   ▼
frontend executes operation → moves cursor + emits over WS:
   {type: "switch_section", section_id: "<id>"}
   │
   ▼
dictation-service WS handler validates section_id ∈ template,
swaps StreamingWindower.base_prompt for the next Whisper window,
emits dictation.section_switched audit.
   │
   ▼
next Final's text is biased by the new section's ASR prompt.
```

The template is **loaded once at session start** (full schema_jsonb
cached on `SessionContext.template_doc`); section-prompt lookups on
swap are in-process (no HTTP round-trip).

The WS protocol stays at **v1** — `SwitchSection` is additive
(ADR-0016 amendment).

## In-process cache (report-service)

`TemplateCache` (cachetools.TTLCache, maxsize=5000, ttl=60s) keyed by
`(tenant_id, template_id)`. Invalidated on PUT/DELETE for the
affected row. Hit ratio metric `mdx_template_cache_hits_total / total`
alerts < 80%.

## Hand-offs

- **Sprint 7 (HF eval):** the 16 system templates are the demo
  catalogue; per-section WER eval is the precursor to sprint 7's
  full nightly eval.
- **Sprint 8 (reports):** `reports.template_id` FK `ON DELETE RESTRICT`;
  `report_versions.content_jsonb.template_version` records the
  schema_version at finalization.
- **Sprint 11 (patients/encounters):** encounters reference reports;
  reports reference templates. Template metadata flows into the
  encounter timeline display.
- **Sprint 13 (anamnesis):** the extractor consumes
  `template.sections[i].field_type` to know which fields to populate;
  sprint-13 adds new field types like `CHOICE`/`MULTI_CHOICE`.
- **Sprint 17 (admin + FHIR):** `templates.metadata.fhir_template`
  maps to FHIR Composition.section structure; admin UI surfaces the
  existing endpoints; re-bind UI for deprecated templates lands here.
