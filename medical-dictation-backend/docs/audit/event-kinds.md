# Audit Event Kinds

Every event written through `AuditWriter` has a `kind` field — a
dotted-string identifier. This catalogue is the source of truth. The
constants live at `services/auth-service/src/auth_service/audit_kinds.py`
(per service); centralising them as a service-local module catches
typos at import.

| kind                              | severity | emitter                          | meaning                                                            |
| --------------------------------- | -------- | -------------------------------- | ------------------------------------------------------------------ |
| `auth.login`                      | info     | auth-service /auth/login         | Successful password-grant exchange                                 |
| `auth.login_failed`               | warn     | auth-service /auth/login *(not yet)* | Failed credential check. **Deferred Day 6+** — needs cross-tenant user lookup. |
| `auth.refresh`                    | info     | auth-service /auth/refresh       | Successful refresh-token rotation                                  |
| `auth.refresh_replay_detected`    | sec      | auth-service /auth/refresh       | Old refresh token replayed after rotation. Sessions force-revoked. |
| `auth.logout`                     | info     | auth-service /auth/logout        | Explicit logout (when Bearer header allowed tenant resolution)     |
| `auth.reauth_succeeded`           | sec      | auth-service POST /auth/reauth   | S14 — step-up: an already-authenticated user re-entered their password, minting a single-use ticket. Payload: purpose. |
| `auth.reauth_failed`              | sec      | auth-service POST /auth/reauth   | S14 — wrong password from a live session. Payload: purpose, kc_status. Repeated failures trip Keycloak's brute-force detector. |
| `auth.account_locked`             | sec      | auth-service /auth/login         | Keycloak rejected the login with account-locked detail (currently surfaced as HTTP 423 + structured log; audit-row TBD) |
| `authz.denied`                    | sec      | auth-service `requires()` dep    | Role/scope check failed. Payload carries action + target_kind + reason. |
| `user.invited`                    | info     | auth-service /admin/users/invite | tenant_admin created a new user                                    |
| `user.deactivated`                | sec      | auth-service /admin/users/{sub}/deactivate | Sessions revoked, status flipped                         |
| `user.reactivated`                | sec      | auth-service /admin/users/{sub}/reactivate | Deactivated user re-enabled; status flipped back to active |
| `user.role_changed`               | sec      | auth-service PUT /admin/users/{sub}/roles | Realm roles changed by tenant_admin. Payload carries old_roles → new_roles. |
| `user.reset_mfa`                  | sec      | *(sprint 16+)*                   | MFA enrolment cleared by admin                                     |
| `tenant.created`                  | sec      | auth-service POST /tenants        | A new clinic/tenant was onboarded; actor becomes owner            |
| `tenant.updated`                  | info     | auth-service PATCH /tenants/{id}   | Tenant profile / branding / contact fields changed                |
| `tenant.logo_updated`             | info     | auth-service PUT /tenants/{id}/logo | Tenant logo uploaded / replaced                                   |
| `tenant.member_added`             | sec      | auth-service POST /tenants/{id}/members | A principal was linked to the tenant with a role             |
| `tenant.member_role_changed`      | sec      | auth-service PATCH /tenants/{id}/members/{sub} | Membership role changed (old → new in payload)        |
| `tenant.member_removed`           | sec      | auth-service DELETE /tenants/{id}/members/{sub} | Membership revoked                                    |
| `tenant.switched`                 | info     | auth-service POST /tenants/{id}/switch | User switched their active tenant                            |
| `audit.chain_verified`            | info/sec | nightly verifier                 | One per tenant per verify run. severity flips to `sec` on divergence |
| `asr.audio_uploaded`              | info     | asr-service POST /asr/jobs       | Audio file enveloped + persisted; row inserted in `audio_files`    |
| `asr.audio_deleted`               | sec      | core-service erasure engine (S11 step 07) | Right-to-erasure crypto-shred of a recording: MinIO object + metadata row (its wrapped DEK) destroyed. Payload: request_id, detail |
| `asr.job_queued`                  | info     | asr-service POST /asr/jobs       | Job durably recorded + enqueued on Redis Streams                   |
| `asr.transcription_started`       | info     | asr-worker processor             | Worker picked the job up; row moved to `running`                   |
| `asr.transcription_complete`      | info     | asr-worker processor             | Inference + encrypted transcript stored; row moved to `complete`   |
| `asr.transcription_failed`        | error    | asr-worker processor             | Job failed with `error_kind` (gpu_oom, corrupt_audio, timeout, …)  |
| `asr.transcript_accessed`         | info     | asr-service GET /asr/jobs/{id}/result | Plaintext transcript served (decrypt-and-return proxy; presigned URLs only ever serve ciphertext). Payload: audio_id, bytes |
| `asr.job_cancelled`               | info     | asr-service DELETE / worker      | Job cancelled by user or by worker honouring cancel_requested      |
| `asr.quota_exceeded`              | warn     | asr-service POST /asr/jobs       | Tenant hit the monthly upload cap                                  |
| `asr.key.master_missing`          | error    | asr-worker startup (fail-closed) | Master key absent/malformed at boot. **System-wide, pre-tenant** — emitted as a CRITICAL structured log, NOT a per-tenant audit-chain row (no tenant context exists). Worker exits non-zero; see runbook § master-key-missing |
| `dictation.session.started`       | info     | dictation-service WS handler     | New streaming session accepted (after auth + capacity)             |
| `dictation.session.resumed`       | info     | dictation-service WS handler     | Existing session reattached after a network drop                   |
| `dictation.session.finalized`     | info     | dictation-service finalize       | Session ended cleanly; transcript + audio persisted                |
| `dictation.session.abandoned`     | info     | dictation-service abandon timer  | Reconnecting > 30 min with no client; resources freed              |
| `dictation.session.failed`        | error    | dictation-service handler        | Worker_failed / opus_fatal / internal                              |
| `dictation.audio.uploaded`        | info     | dictation-service finalize       | End-of-session WAV encrypted + stored to MinIO                     |
| `dictation.audio.truncated`       | warn     | dictation-service finalize       | tmpfs ring wrapped; audio file shorter than total received         |
| `dictation.upgrade.failed`        | warn/sec | dictation-service ws upgrade     | Auth / rate-limit / subprotocol / origin rejection. sec on repeats |
| `voice_command.executed`          | info     | frontend (forwarded)             | Sprint 05 — clinician's intent fired in the editor                 |
| `voice_command.undone`            | warn     | frontend (forwarded)             | Sprint 05 — clinician undid the fired intent within 600 ms         |
| `voice_command.executed_failed`   | warn     | frontend (forwarded)             | Sprint 05 — command's referenced state (template section) gone     |
| `abbreviation.policy.set`         | info     | nlp-service PUT /nlp/abbreviations | Sprint 05 — tenant admin upserted an abbreviation rule           |
| `abbreviation.policy.deleted`     | info     | nlp-service DELETE /nlp/abbreviations/{id} | Sprint 05 — tenant admin removed an abbreviation rule    |
| `dictation.nlp_timeout`           | warn     | dictation-service NlpClient      | Sprint 05 — NLP call exceeded 200 ms; emitted raw Whisper text    |
| `template.cloned`                 | info     | report-service POST /templates/clone | Sprint 06 — tenant cloned a system or own template            |
| `template.updated`                | info     | report-service PUT /templates/{id} | Sprint 06 — cosmetic edit; same row, schema_version bumped       |
| `template.versioned`              | info     | report-service PUT /templates/{id} | Sprint 06 — structural edit; new row with parent_template_id     |
| `template.deprecated`             | info     | report-service DELETE /templates/{id} | Sprint 06 — soft-delete; status='deprecated'                 |
| `template.viewed_full`            | info     | report-service GET /templates/{id} | Sprint 06 — full schema_jsonb fetched                          |
| `dictation.section_switched`      | info     | dictation-service WS handler     | Sprint 06 — section navigation; prompt swap for next window      |
| `template.created`                | info     | report-service POST /templates   | M1 — plain create of a tenant template (vs clone). Payload: code, specialty |
| `report.created`                  | info     | report-service POST /v1/reports (+ /from-transcript) | Sprint 08 — draft report created. Payload: code, version_id |
| `report.draft.updated`            | info     | report-service PUT /v1/reports/{id}/draft | Sprint 08 — autosave, AGGREGATED per dictation session (not per keystroke). Payload: version_number, dictation_session_id |
| `report.reverted`                 | info     | report-service POST /v1/reports/{id}/revert | Sprint 08 — finalized → draft inside the 1 h author window. |
| `report.cancelled`                | info     | report-service POST /v1/reports/{id}/cancel | Sprint 08 — report cancelled. Payload: reason |
| `report.amended`                  | info     | report-service (post-sign, sprint 09) | Sprint 08/09 — amendment signed; status → amended. |
| `report.amendment_drafted`        | info     | report-service POST /v1/reports/{id}/amend | Sprint 08 — amendment version created on a signed report (pre-sign). Payload: amendment_type, version_number |
| `report.viewed_full`              | info     | report-service GET /v1/reports/{id} | Sprint 08 — non-author full read; carries the declared `purpose`. |
| `report.searched`                 | info     | report-service GET /v1/reports/search | Sprint 08 — search executed. Payload: filter shape only, never the query text. |
| `report.chain_integrity_failure`  | sec      | report-service chain reconciler / property test | Sprint 08 — an append-only version chain anomaly was detected. Investigate immediately. |
| `report.pdf_rendered`             | info     | report-service GET /v1/reports/{id}/pdf | M1 — unsigned PDF rendered for local KEP. Payload: version_number, size_bytes, purpose |
| `phi_access.granted`              | sec      | report-service POST /v1/phi-access-requests | S14 break-glass — an admin was granted time-limited access to ONE report. Payload: grant_id, reason_code, reason_note, expires_at, patient_id. The note is staff-authored justification and belongs in the chain; it is deliberately NOT forwarded to notifications. |
| `phi_access.denied`               | sec      | report-service POST /v1/phi-access-requests | S14 — a break-glass request refused. Payload: reason_code, cause (`reauth_ticket_invalid`). |
| `phi_access.used`                 | sec      | report-service GET /v1/reports/{id} and /pdf | S14 — a read performed UNDER a grant, emitted alongside `report.viewed_full` so break-glass reads are one query rather than a filter over every view ever recorded. Payload: grant_id, reason_code, surface. |
| `phi_access.revoked`              | sec      | report-service POST /v1/phi-access-requests/{id}/revoke | S14 — an open grant closed early. Payload: grant_id, reason_code. |
| `report.completed`                | info     | report-service POST /v1/reports/{id}/finalize | M1 — finalize completion summary (paired with `report.finalized`). Payload: version_number, section_count, low_confidence_count, source_session_id |
| `signing.session.cancelled`       | info     | signing-service DELETE /signing/sessions/{id} | M1 — user aborted an in-flight session. Payload: from_status |
| `signing.session.local_upload`    | info     | signing-service POST /signing/sessions/{id}/upload | M1 — locally-signed PAdES uploaded + verified (paired with `signing.envelope.persisted`). Payload: provider, signed_envelope_id, is_qualified |
| `signing.file_key_rejected`       | sec      | signing-service POST /signing/inline | S09-rev — file-key container/password rejected (bad container or wrong password). Payload: reason |
| `signing.dev_password_rejected`   | sec      | signing-service POST /signing/inline | S09-rev — dev-scaffold account-password re-auth rejected or locked. Payload: reason |
| `report.sign_requested`           | info     | report-service POST /v1/reports/{id}/sign | S09-rev — sign surface invoked (before delegation to signing-service). Payload: provider, resource_type |
| `report.synthesis_started`        | info     | report-service POST /v1/reports/{id}/synthesize | Spec item 1 — synthesis run begun. Payload: section_count, language, provider |
| `report.synthesis_completed`      | info     | report-service POST /v1/reports/{id}/synthesize | Spec item 1 — synthesis run finished (paired with `report.synthesis_started`). Payload: job_id, section_count, language, provider |
| `patient.created`                 | info     | core-service POST /patients      | Sprint 11 — new patient added to the roster. Payload: has_mrn |
| `patient.updated`                 | info     | core-service PUT /patients/{id}  | Sprint 11 — patient demographics edited. Payload: fields (changed column names) |
| `patient.viewed`                  | info     | core-service GET /patients/{id}  | Sprint 11 — full patient record fetched (PHI access). |
| `encounter.created`              | info     | core-service POST /patients/{id}/encounters | Sprint 11 — encounter recorded. Payload: encounter_id, kind |
| `note.created`                    | info     | core-service POST /notes         | Sprint 11 — clinical note created. Payload: patient_id, structure |
| `note.updated`                    | info     | core-service PATCH /notes/{id}   | Sprint 11 — draft note edited. |
| `note.signed`                     | info     | core-service POST /notes/{id}/sign | Sprint 11 — note signed (becomes immutable). |
| `consent.granted`                 | info     | core-service POST /patients/{id}/consents | Sprint 11 — consent recorded. Payload: consent_id, type |
| `consent.withdrawn`               | info     | core-service POST /patients/{id}/consents/{cid}/withdraw | Sprint 11 — consent withdrawn. Payload: consent_id |
| `consent.signed`                  | info     | core-service POST /patients/{id}/consents/{cid}/sign | S11 step 03 — КЕП envelope linked to a digital consent (inline tiers; the envelope itself is audited by signing-service's `signing.envelope.persisted`). Payload: consent_id, envelope_id, signature_level, is_qualified |
| `anamnesis.updated`               | info     | core-service PUT /patients/{id}/anamnesis | Sprint 11 — structured history saved. |
| `anamnesis.field.extracted`       | info     | report-service POST /v1/reports/{id}/finalize | Sprint 13 — ONE aggregated row per finalized report: how many typed fields still carried machine-extracted values at finalize. Deliberately not per-utterance (chain pollution). Payload: field_types (list), section_count. **No values, no prose.** |
| `anamnesis.field.confirmed`       | info     | report-service PUT /v1/reports/{id}/draft | Sprint 13 — a clinician confirmed an extracted typed-field value (extracted→manual with the same value, or a proposed ICD-10 code entering `section.icd10`). Payload: section_key, field_type, and for CLOSED vocabularies only: selected (option slugs) or codes (ICD-10). **Never free text.** |
| `anamnesis.field.overridden`      | info     | report-service PUT /v1/reports/{id}/draft | Sprint 13 — a clinician REPLACED an extracted value with a different one; the extractor-quality signal behind step-08's override-rate dashboard. Payload: section_key, field_type, selected/was (slugs) or codes/proposed (ICD-10). **Never free text** — a free-text override records its section and type only. |
| `privacy.dsar_requested`          | sec      | core-service POST /patients/{id}/dsar | Sprint 11 — data-subject access request logged. Payload: request_id, kind |
| `privacy.erasure_scheduled`       | sec      | *(superseded S11 step 04)* | Historical (S11-M2): emitted when erasure requests auto-scheduled at creation. Replaced by `privacy.erasure_requested` + `privacy.erasure_approved`; existing chain rows remain valid. |
| `privacy.erasure_requested`       | sec      | core-service POST /patients/{id}/erasure | S11 step 04 — erasure requested; awaits second-person approval. Payload: request_id, kind |
| `privacy.erasure_reviewed`        | info     | core-service POST /privacy-requests/{id}/review | S11 step 04 — request marked under review. Payload: request_id |
| `privacy.erasure_approved`        | sec      | core-service POST /privacy-requests/{id}/approve | S11 step 04 — second-person approval; grace period starts. Payload: request_id, scheduled_for, grace_days |
| `privacy.erasure_rejected`        | sec      | core-service POST /privacy-requests/{id}/reject | S11 step 04 — rejected/cancelled with written reason (incl. during grace). Payload: request_id, rejection_reason |
| `dsar.export.completed`           | sec      | core-service DSAR engine (background task) | S11 step 06 — package assembled + stored. Payload: request_id, item_count, package_sha256. (`privacy.dsar_requested` covers the request — one canonical set.) |
| `dsar.export.failed`              | sec      | core-service DSAR engine | S11 step 06 — export failed; row → 'failed'. Payload: request_id, error_class |
| `dsar.download.link_issued`       | sec      | core-service GET /privacy-requests/{id} | S11 step 06 — a download pointer was minted (per status call). Payload: request_id |
| `dsar.package.downloaded`         | sec      | core-service GET /privacy-requests/{id}/download | S11 step 06 — the package was actually served (decrypt-and-stream). Payload: request_id, bytes |
| `erasure.executing`               | sec      | core-service erasure engine | S11 step 07 — execution started (or resumed after a crash). Payload: request_id, operator, inventory_counts |
| `erasure.artifact_destroyed`      | sec      | core-service erasure engine | S11 step 07 — one artifact destroyed (kind+id in target; ids only, never identity strings). Payload: request_id, detail |
| `erasure.executed`                | sec      | core-service erasure engine | S11 step 07 — request completed; report_of_execution written. Emitted exactly once per completion. Payload: request_id, destroyed, retained, engine_version |
| `demo.rate_limit_hit`             | warn     | `libs/demo` rate limiter         | Sprint 07 — a demo request was rejected by the three-axis limiter (per-IP / per-user / per-session). |
| `demo.session_capped`            | warn     | `libs/demo` rate limiter         | Sprint 07 — demo session duration exceeded the per-session cap. |
| `demo.daily_minutes_capped`      | warn     | `libs/demo` rate limiter         | Sprint 07 — per-user daily wall-clock minute budget exhausted. |
| `demo.ip_blocked`                | warn     | `libs/demo` rate limiter         | Sprint 07 — an IP repeatedly hit caps and entered cooldown. |
| `demo.privacy_test_passed`       | sec      | `scripts/eval/run_daily_privacy_test.py` | Sprint 07 — daily privacy release-gate confirmed no audio at rest. |
| `demo.privacy_test_failed`       | sec      | `scripts/eval/run_daily_privacy_test.py` | Sprint 07 — daily privacy gate found residual audio; pages DPO + security. |
| `eval.run.started`               | info     | `scripts/eval/run_wer.py`        | Sprint 07 — a WER eval run began (structured log; non-tenant CI event). |
| `eval.run.completed`             | info     | `scripts/eval/run_wer.py`        | Sprint 07 — WER eval run finished; scores recorded to `audit.eval_runs`. |
| `eval.run.regressed`             | warn     | `scripts/eval/compare_to_baseline.py` | Sprint 07 — a run breached a baseline threshold (WER/RTF/number-norm); Slacks `#eval-regressions`. |

> **Demo + eval kinds (sprint 07)** are *not* hash-chained `audit.events`
> rows — they are non-tenant, system-level events surfaced via structured
> logs, Prometheus gauges, and Slack alerts. Their constants live in
> `libs/demo/src/demo/audit_kinds.py` (`DEMO_AUDIT_KINDS`) and
> `scripts/eval/audit_kinds.py` (`EVAL_AUDIT_KINDS`).

## Autocomplete (sprint 10 — autocomplete-service)

Tenant-scoped, hash-chained. Constants in
`services/autocomplete-service/src/autocomplete_service/audit_kinds.py`
(also listed in `docs/audit/audit-kinds-sprint-10.md`).

| kind                                      | severity | emitter         | meaning                                              |
| ----------------------------------------- | -------- | --------------- | ---------------------------------------------------- |
| `autocomplete.phrase.created`             | info     | phrases router  | Personal/tenant phrase added (`source`, `language`).  |
| `autocomplete.phrase.updated`             | info     | phrases router  | Phrase changed (`source`, fields_changed).           |
| `autocomplete.phrase.deleted`             | info     | phrases router  | Phrase soft-deleted.                                 |
| `autocomplete.phrase.write_rejected_pii`  | sec      | phrases router  | Write rejected by the PII scrubber (`patterns`).     |
| `autocomplete.snippet.created`            | info     | snippets router | Snippet added (`source`, `trigger`).                 |
| `autocomplete.snippet.updated`            | info     | snippets router | Snippet changed (`trigger`).                         |
| `autocomplete.snippet.deleted`            | info     | snippets router | Snippet removed (`trigger`).                         |
| `autocomplete.rollup.completed`           | info     | roll-up job     | Nightly counter roll-up done (`rollup_date`, `phrases_updated`). |

## Adding a new kind

1. Define the constant in `services/<service>/src/<service>/audit_kinds.py`.
2. Use it via `await audit_writer.write_event(kind=audit_kinds.X, ...)`.
3. Add a row to this table.
4. If the kind warrants its own dashboard panel or alert rule, add
   them in `infra/grafana/dashboards/` and `infra/prometheus/rules/`.

### Sprint 12 — notifications

| kind                              | severity | emitter                          | meaning                                                            |
| --------------------------------- | -------- | -------------------------------- | ------------------------------------------------------------------ |
| `notification.materialized`       | info     | notification-service ingest consumer | One event fanned out to N per-recipient rows. Payload: category, event_id, created/coalesced/duplicates counts. |
| `notification.coalesced`          | warn     | notification-service materialize | Storm cap tripped; same-category events folded into one row (E1). |
| `notification.delivered`          | info     | notification-service delivery worker | A channel dispatched successfully. Payload: channel, attempts. |
| `notification.suppressed`         | info     | notification-service materialize | A channel was deliberately NOT dispatched. Payload carries the reason (preference / quiet_hours / no_email_address / digest_deferred) — the auditable proof for E8. |
| `notification.delivery_failed`    | warn     | notification-service delivery worker | An attempt failed and will be retried with backoff. |
| `notification.dead_lettered`      | error    | notification-service delivery worker | Retries exhausted, or a permanently-undeliverable envelope. Someone will never be told something. |
| `notification.read`               | info     | notification-service feed router | User marked a notification read. |
| `notification.preferences_updated`| info     | notification-service preferences router | User changed their own notification preferences. |
| `notification.digest_sent`        | info     | notification-service digest job  | Daily digest email sent. Payload: digest_date, included count. |

## Payload conventions

The `payload` arg to `write_event` is the caller-supplied dict that lands
*inside* the canonicalised event record under the `payload` key. Keep it
shallow (no deeply nested objects) and pre-convert non-JSON types
(UUID → str, datetime → ISO-8601). The writer's `_normalize_payload`
handles UUID/datetime/bytes for you.

Sensitive values (passwords, raw OTP codes, PHI) **must not** appear in
the payload. Audit is for *who did what when* — the *what* references
IDs, not contents.


## Sprint-13 reconciliation (2026-07-23)

The three anamnesis kinds above are all **info**, including
`anamnesis.field.overridden` — an override is a quality signal about
the extractor, not a security event, and filing it as `sec` would
dilute the security severity's meaning.

### Deviation: `icd10.searched` is metrics-only

The sprint-13 plan listed an `icd10.searched` audit kind for
`GET /v1/icd10/search`. **It is deliberately not implemented.** That
endpoint sits in the diagnosis picker's typing path, so it fires on
substantially every keystroke; a hash-chained, append-only row per
keystroke is chain pollution that would bury the clinically meaningful
events around it. The path is instrumented with metrics instead
(`mdx_icd10_searches_total`, `mdx_icd10_search_seconds`), which answer
the same operational questions — volume, latency, zero-result rate —
without touching the audit chain.

Precedent: the sprint-10 autocomplete suggest path made exactly this
call for exactly this reason. Rationale also recorded in
`docs/runbooks/icd10.md`.

**What is still audited** about ICD-10: the clinically meaningful act
of a code entering a report — `anamnesis.field.confirmed` /
`anamnesis.field.overridden` carry the codes. Searching is not a
clinical act; choosing is.
