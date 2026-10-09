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
| `user.reset_mfa`                  | sec      | auth-service DELETE /auth/mfa/{sub} | S16 — MFA enrolment cleared by admin (secret attributes wiped, sessions revoked). Payload: sessions_revoked. |
| `user.mfa_reminded`               | sec      | auth-service POST /admin/users/{sub}/mfa-reminder | S21 — an access review asked a user to enrol MFA. The only write an `auditor` can make; it changes nothing about the account. Payload: reminder_count, first_reminded_at. Pairs with `auth.mfa.enrolled` to answer "how long did this account sit unprotected after we noticed". |
| `auth.mfa.enrolled`               | info     | auth-service POST /auth/mfa/verify | S16 — TOTP enrolment completed (first valid code). Resolves any open `mfa_reminders` row. |
| `auth.session.revoked`            | info/sec | auth-service logout / replay / deactivate / POST /auth/sessions/revoke-all | S16 — sid or sub pushed onto the revocation denylist (ADR-0040). Payload: reason (+ sid for logout). sec on refresh_replay / user.deactivated, info on plain logout. The self-service "sign out everywhere" in settings emits it with `scope=all, initiated_by=self`. |
| `auth.password.reset_requested`   | sec      | auth-service POST /auth/password/forgot | Password-reset link minted and queued. Payload: ip_hash (salted). **Emitted only for an address that resolves to an active account** — writing one for an unknown address would rebuild the enumeration oracle the endpoint's uniform 202 exists to prevent. |
| `auth.password.reset_completed`   | sec      | auth-service POST /auth/password/reset | A mailed token was redeemed and the password replaced. Every session revoked and all other outstanding tokens spent. Payload: ip_hash. |
| `auth.password.changed`           | sec      | auth-service POST /auth/password/change | A signed-in user changed their own password after re-entering the current one. Same tail as reset: sessions revoked, tokens spent, notification queued. Payload: ip_hash. A *wrong* current password emits `auth.reauth_failed` with `purpose=password_change`. |
| `auth.account.lockdown`           | sec      | auth-service POST /auth/security/lockdown | The "this wasn't me" button in the security-notification email. Highest-signal event this service emits: a user asserting their account was taken over. Payload: ip_hash, sessions_revoked (false = Keycloak or the denylist refused; investigate). |
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
| `asr.transcription_failed`        | error    | asr-worker processor             | Job failed with `error_kind` — the closed vocabulary in `asr_models.errors` (`docs/api/asr-job-errors.md`). Payload carries `error_kind` + a truncated `detail` |
| `asr.transcription_failed`        | warn     | asr-service reaper               | Same kind, `severity=warn`, `payload.actor="reaper"`: a job stranded by a dead worker (`worker_lost`) or a message that never reached one (`queue_lost`), closed out from outside the worker |
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
| `template.rebound`                | info     | report-service POST /templates/{id}/rebind | Sprint 17 — one draft report moved to a successor template. Payload: report_id, from_template_id, to_template_id (ids only, no PHI) |
| `dictation.section_switched`      | info     | dictation-service WS handler     | Sprint 06 — section navigation; prompt swap for next window      |
| `dictation.nlp_timeout`           | warn     | dictation-service finalize       | Sprint 05 contract, wired in S14 — NLP pipeline unavailable at finalize; the raw transcript is persisted unchanged |
| `conversation.speaker_mapping.inferred` | info | dictation-service WS handler   | Sprint 14 — doctor/patient mapping hypothesis emitted or changed. Payload: mapping, confidence, rationale |
| `conversation.speaker_mapping.manual_set` | info | dictation-service WS handler | Sprint 14 — clinician's manual assignment; inference frozen from here on. Payload: mapping |
| `conversation.consent_refused`    | warn     | dictation-service WS handler     | Sprint 14 — conversation start refused: no encounter, or no granted `recording` consent for its patient |
| `conversation.draft.created`      | info     | dictation-service finalize       | Sprint 14 — report draft created from a conversation session via report-service POST /v1/reports. Payload: session_id, code, version_id, segments |
| `conversation.draft.create_failed`| warn     | dictation-service finalize       | Sprint 14 — draft creation skipped/failed; the transcript is already persisted. Payload: reason |
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
| `phi_access.granted`              | sec      | report-service POST /v1/phi-access-requests | S14 break-glass — a principal was granted time-limited access to ONE report or patient. **HOTFIX:** no longer admin-only — a clinician with no treatment relationship to the patient mints the same grant, so `actor_role` is load-bearing here. This is the kind the hotfix spec calls `access.break_glass`; the established name is kept so the S14 oversight surface and `phi_access.*` filters keep working rather than fragmenting across two vocabularies. Payload: grant_id, reason_code, reason_note, expires_at, patient_id. The note is staff-authored justification and belongs in the chain; it is deliberately NOT forwarded to notifications. |
| `phi_access.denied`               | sec      | report-service POST /v1/phi-access-requests | S14 — a break-glass request refused. The hotfix spec's `access.break_glass_denied`. Payload: reason_code, cause (`reauth_ticket_invalid`). |
| `authz.denied`                    | sec      | report-service `_phi_access_guard`, core-service `_phi_access_guard` | S14 + **HOTFIX** — a refused attempt to open one report or one patient record, emitted by the break-glass guards before the 403. Payload: action (`report.read` / `patient.read_full`), reason — one of `role_denied` (the role holds neither standing read nor break-glass), `no_relationship` (HOTFIX: standing read, but no treatment relationship with this patient and no live grant), `no_live_grant` (no standing read, no grant). `no_relationship` is the one to watch: it is a clinician reaching for a chart that is not theirs. |
| `phi_access.used`                 | sec      | report-service GET /v1/reports/{id} and /pdf | S14 — a read performed UNDER a grant, emitted alongside `report.viewed_full` so break-glass reads are one query rather than a filter over every view ever recorded. Payload: grant_id, reason_code, surface. |
| `phi_access.revoked`              | sec      | report-service POST /v1/phi-access-requests/{id}/revoke | S14 — an open grant closed early. Payload: grant_id, reason_code. |
| `report.completed`                | info     | report-service POST /v1/reports/{id}/finalize | M1 — finalize completion summary (paired with `report.finalized`). Payload: version_number, section_count, low_confidence_count, source_session_id |
| `signing.session.cancelled`       | info     | signing-service DELETE /signing/sessions/{id} | M1 — user aborted an in-flight session. Payload: from_status |
| `signing.session.local_upload`    | info     | signing-service POST /signing/sessions/{id}/upload | M1 — locally-signed PAdES uploaded + verified (paired with `signing.envelope.persisted`). Payload: provider, signed_envelope_id, is_qualified |
| `signing.file_key_rejected`       | sec      | signing-service POST /signing/inline | S09-rev — file-key container/password rejected (bad container or wrong password). Payload: reason |
| `signing.denied_role`             | sec      | signing-service `assert_may_sign` (sessions / inline / uploads) | **HOTFIX** — a principal without `report.sign` reached a signing surface. Emitted BEFORE any КЕП provider is dispatched to, so it records the attempt rather than a partial signature. Payload: roles (the caller's FULL role list, not just the primary — "which role did they think authorised this" is the first review question), required_permission, resource_kind. Also increments `mdx_signing_denied_total`, which the `SigningDeniedByRoleRepeated` alert watches. |
| `signing.dev_password_rejected`   | sec      | signing-service POST /signing/inline | S09-rev — dev-scaffold account-password re-auth rejected or locked. Payload: reason |
| `report.sign_requested`           | info     | report-service POST /v1/reports/{id}/sign | S09-rev — sign surface invoked (before delegation to signing-service). Payload: provider, resource_type |
| `report.synthesis_started`        | info     | report-service POST /v1/reports/{id}/synthesize | Spec item 1 — synthesis run begun. Payload: section_count, language, provider |
| `report.synthesis_completed`      | info     | report-service POST /v1/reports/{id}/synthesize | Spec item 1 — synthesis run finished (paired with `report.synthesis_started`). Payload: job_id, section_count, language, provider |
| `patient.created`                 | info     | core-service POST /patients      | Sprint 11 — new patient added to the roster. Payload: has_mrn |
| `patient.updated`                 | info     | core-service PUT /patients/{id}  | Sprint 11 — patient demographics edited. Payload: fields (changed column names) |
| `patient.viewed`                  | info     | core-service GET /patients/{id}  | Sprint 11 — full patient record fetched (PHI access). |
| `patient.imported`                | info     | core-service POST /patients/import | Bulk roster import — one event per request, alongside a `patient.created` per written row. Payload: total, created, skipped, failed, dry_run |
| `patient_document.uploaded`       | info     | core-service POST /patients/{id}/documents | Migration 0065 — file attached to a patient record. Payload: patient_id, category, content_type, byte_size, break_glass |
| `patient_document.downloaded`     | info/sec | core-service GET /patients/{id}/documents/{doc}/content | Migration 0065 — attachment read (PHI access; `sec` under break-glass). Payload: patient_id, byte_size, break_glass |
| `patient_document.deleted`        | sec      | core-service DELETE /patients/{id}/documents/{doc} | Migration 0065 — attachment crypto-shredded (object first, then row). Payload: patient_id, category, break_glass |
| `encounter.created`              | info     | core-service POST /patients/{id}/encounters | Sprint 11 — encounter recorded. Payload: encounter_id, kind |
| `encounter.started`               | info     | core-service POST /encounters/{id}/start | Migration 0058 — scheduled visit went live. Payload: encounter_id, from, to |
| `encounter.paused`                | info     | core-service POST /encounters/{id}/pause | Migration 0058 — visit paused (clinician stepped out). Payload: encounter_id, from, to, reason? |
| `encounter.resumed`               | info     | core-service POST /encounters/{id}/resume | Migration 0058 — paused visit resumed. Payload: encounter_id, from, to |
| `encounter.completed`             | info     | core-service POST /encounters/{id}/complete | Migration 0058 — visit ended; stamps patients.last_visit_at. Payload: encounter_id, from, to, reason?, forced_over_live_sessions? |
| `encounter.cancelled`             | info     | core-service POST /encounters/{id}/cancel | Migration 0058 — visit abandoned. Payload: encounter_id, from, to, reason?, forced_over_live_sessions? |
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
| `report.audio_replayed`           | info     | report-service POST /v1/audio-clips | Sprint 15 (ADR-0037) — a replay clip was created. Payload: clip_id, source_kind, start_ms, end_ms, purpose, is_author, break_glass. Sec severity when under break-glass. |
| `layer_c.completion.shown`        | info     | generation-service shown-audit buffer | Sprint 15 (ADR-0036) — AGGREGATED: one row per tenant per flush interval counting served inline completions. Payload: count. Per-keystroke rows would pollute the chain. |
| `layer_c.completion.filtered`     | warn     | generation-service POST /v1/completions/inline | Sprint 15 — the output safety filter dropped a completion that introduced a clinical value absent from the typed text. Payload: section_key, reason (pattern class), matched (the offending fragment — closed class, never prose), language. |
| `search.expanded`                 | info     | report-service search-audit buffer | Sprint 15 (ADR-0038) — AGGREGATED: one row per tenant per flush interval counting synonym-expanded searches. Payload: count, expanded_terms_total. Never the query text. |
| `synonym.group.created`           | info     | report-service POST /v1/synonyms | Sprint 15 — tenant synonym group added. Payload: group_id, term_count, language. Terms are closed-vocabulary dictionary entries, not prose. |
| `synonym.group.updated`           | info     | report-service PUT /v1/synonyms/{group_id} | Sprint 15 — tenant synonym group replaced. Payload: group_id, term_count, language. |
| `synonym.group.deleted`           | info     | report-service DELETE /v1/synonyms/{group_id} | Sprint 15 — tenant synonym group removed. Payload: group_id. |

## Sprint-16 — KMS, schedulers, backup horizon

| kind | severity | emitter | meaning |
|------|----------|---------|---------|
| `kms.rewrap.completed` | sec | `scripts/kms/rewrap-tenant-keks.py` | one tenant KEK re-wrapped file→Vault (ADR-0011 amendment). Payload: from, to master ids. |
| `scheduler.job.completed` | info | report-/autocomplete-/core-service job loops (ADR-0041) | one scheduler iteration finished; written under the reserved global tenant. Payload: per-job counts. |
| `scheduler.job.failed` | warn | same | an iteration raised; the loop survives and retries next interval. |
| `erasure.backup_horizon_reached` | sec | core-service backup-horizon job | `backups_purged_by` passed; `report_of_execution` gained its "fully purged from backups" line. Payload: request_id. |

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

## EVA-S01 — evidence module (no kinds added)

Sprint EVA-S01 (domain model & contracts, `evidence-backend` workspace) adds
**no audit kinds**: it ships contracts, schema and permissions only — no
runtime write path exists yet. The `evidence.probe_action` kind planned by the
EVA-S00 spec was never registered because S00's runtime slice was not built
(recorded as an as-built delta in `evidence-backend/docs/sprints/sprint-01.md`);
first real evidence kinds arrive with the first evidence service (EVA-S02+).

## EVA-S02 — evidence ingestion (`evidence-ingest`)

Constants: `evidence-backend/services/evidence-ingest/src/evidence_ingest/audit_kinds.py`.

| kind | severity | emitter | meaning |
|---|---|---|---|
| `evidence.document_ingested` | info | ingest pipeline (indexing stage) | new document or version parsed, chunked, embedded and indexed |
| `evidence.document_retracted` | sec | retractions processor | retraction flagged; document excluded from the next snapshot (kept for provenance) |
| `evidence.document_quarantined` | sec | ingest-time injection screen (LM4) | instruction-pattern payload held for knowledge_admin review |
| `evidence.quarantine_decided` | sec | quarantine review endpoint | reviewer approved (pipeline resumes) or rejected (job dead) |
| `evidence.snapshot_created` | info | snapshot builder | immutable corpus snapshot frozen (member list + license exclusions in payload) |

`authz.denied` emissions from evidence services reuse the platform kind.

## EVA-S03 — evidence retrieval (no kinds added; recorded decision)

`evidence-retrieval` performs reads only and emits **no audit events at this
layer** (spec §8): retrieval requests carry no user identity (service-token
hop; identity attaches in evidence-answer), and answer-level provenance
(ET1, `answer_provenance`) is the durable record of which passages were
used. Revisiting this (e.g. auditing raw retrieval for research-use
telemetry) is an ADR, not a quiet addition.

## EVA-S04 — Quick Search (`evidence-answer`, `evidence-websearch`)

Constants: `evidence-backend/services/evidence-answer/src/evidence_answer/audit_kinds.py`
and `…/evidence-websearch/src/evidence_websearch/audit_kinds.py`.

| kind | severity | emitter | meaning |
|---|---|---|---|
| `evidence.answer_generated` | info | `evidence-answer` persist stage | an answer was stored; payload: `answer_id`, `mode`, `status`, contributing `connectors`, `verified=false` (until S06) |
| `evidence.question_deflected` | info | `evidence-answer` triage stage | a question was refused; payload: `reason_code`, `matched_rule`, `classifier_used` |
| `evidence.web_domain_added` | sec | `evidence-websearch` domains admin | fetch allowlist entry created or re-enabled; payload: `domain`, `trust_tier`, `status`, `metadata_only` |
| `evidence.web_domain_disabled` | sec | `evidence-websearch` domains admin | allowlist entry disabled for the tenant |

Two payload decisions worth knowing:

- **The question text is never in a deflection payload.** Audit payloads are
  widely readable, and a deflected question — an emergency, a self-harm
  disclosure, a patient asking about their own body — is the single most
  likely one to carry something sensitive. The reason code and the rule id
  are enough to explain the deflection and to count it.
- **`evidence.answer_generated` is not best-effort.** If the audit write
  fails, the `answers` row is deleted rather than left un-audited: an answer
  nobody can account for is worse than no answer. (The envelope object is
  left behind deliberately — an orphan blob is inert; the row is what makes
  it reachable.)

The question/answer *content* is in `questions`/`answers` under user-private
RLS and, for the envelope, inside an `EncryptedObjectStore` object — not in
the audit chain.

## Sprint 21 — clinical corpus pipeline (`corpus-forge`)

Constants: `services/corpus-forge/src/corpus_forge/audit_kinds.py`.
All corpus events are fleet-level operator actions and are written under
the **reserved global tenant** (nil UUID, migration 0068) via
`MDX_CORPUS_AUDIT_DSN`; in dev, with the DSN unset, they are logged but
not chained (the CLI warns).

| kind | severity | emitter | meaning |
|---|---|---|---|
| `corpus.mining_run` | info | `corpus-forge mine` | one mining execution; payload: `run_id`, `query_sha256` (must equal the DPO-signed value in docs/signoffs/sprint-21-dpo.md), gram/insert/drop counts, k-anonymity parameters |
| `corpus.candidate_generated` | info | `corpus-forge import` / `generate` | a batch of candidates entered staging; payload: source/batch id, counts incl. `dropped_malformed` (generation is dropped-not-repaired) |
| `corpus.candidate_reviewed` | info | `corpus-forge review` / autocomplete-service `POST /corpus/candidates/{id}/review` | one human decision; payload: `candidate_id`, `decision`, `mode` (`review` or spot-`audit`), `latency_ms` (the 15 s/decision instrument). The HTTP path writes under the reviewer's tenant chain; the CLI under the global tenant. |
| `corpus.auto_accepted` | info | `corpus-forge jury` | a machine acceptance; payload: `candidate_id`, `engine` (`jury:<model>:<prompt_version>`), `tier` — auto-accept exists only after the calibration gate |
| `corpus.jury_disagreement` | info | `corpus-forge jury` | non-unanimous jury vote; the stream that tells us whether the jury is coherent |
| `corpus.release_published` | info | `corpus-forge release` | immutable release registered; payload: `version`, `phrase_count`, `manifest_sha256` |
| `corpus.candidate_submitted` | info | autocomplete-service `POST /corpus/candidates` | a console-authored (typed or dictated) phrase entered the review queue as a global candidate; payload: `candidate_id`, `language`, `capture` (`typed`/`dictated`), `text_length` — written under the submitter's tenant chain |
| `corpus.candidates_promoted` | info | autocomplete-service `POST /corpus/promote` | accepted global candidates published into the serving corpus (system-scope phrases); payload: `promoted`, `inserted`, `requested_ids` (count or null for promote-all) — release manifests remain a `corpus-forge release` operator action |
| `corpus.eval_take_saved` | info | autocomplete-service `PUT /corpus/eval/takes/{script_id}` | a WER eval recorder take stored or replaced server-side (migration 0089); payload: `script_id`, `condition`, `duration_ms`, `size_bytes` — never audio, never text |
| `corpus.eval_take_deleted` | info | autocomplete-service `DELETE /corpus/eval/takes/{script_id}` | a stored eval take removed; payload: `script_id` |
| `corpus.eval_exported` | info | autocomplete-service `GET /corpus/eval/export` | the tenant's eval takes downloaded as one `eval/corpus/v1/`-shaped archive (the repo-commit hand-off); payload: `utterances` (count), `snapshot_version` (null for the live set) |
| `corpus.eval_line_added` | info | autocomplete-service `POST /corpus/eval/script` | a recording line authored in the console (migration 0091); payload: `script_id`, `language`, `specialty`, `subset`, `source` — the line's text is not in the payload |
| `corpus.eval_line_updated` | info | autocomplete-service `PATCH /corpus/eval/script/{script_id}` | an authored line edited; payload: `script_id`, `subset`. Vendored lines are refused (409) and never appear here |
| `corpus.eval_line_deleted` | info | autocomplete-service `DELETE /corpus/eval/script/{script_id}` | an authored line and its take removed; payload: `script_id` |
| `corpus.eval_adhoc_captured` | info | autocomplete-service `POST /corpus/eval/adhoc` | audio recorded before its text existed, with the gold text written down afterwards; payload: `script_id`, `language`, `specialty`, `subset`, `duration_ms`, `no_patient_data_attested`. **The attestation is why this kind is separate**: it is the only control over patient names on the one path the scripted-only invariant cannot cover, so it needs a named actor and a chained record, not a boolean inside a generic event |
| `corpus.eval_published` | info | autocomplete-service `POST /corpus/eval/publish` | the current takes frozen as an immutable numbered snapshot after a PII sweep of the whole set; payload: `version`, `utterances`, `manifest_sha256` |
| `corpus.eval_imported` | info | autocomplete-service `POST /corpus/eval/import` | replicas bulk-authored from a §6 CSV (migration 0092); payload: `filename`, `file_sha256`, `dry_run`, `rows_total`, `rows_added`, `rows_skipped`, `rows_rejected`, `allow_test` — never a row's text. **Dry runs are audited too**: "we previewed importing 86 lines into the holdout and did not commit" is precisely the event worth having when the test set later looks larger than it should, and `allow_test` records who authorised writing into the frozen set |
| `corpus.eval_gold_revised` | info / **warn** | autocomplete-service `POST /corpus/eval/gold-lint/apply` | gold transcripts rewritten into the spoken form the style guide requires (corpus-v3 Epic B, migration 0093); payload: `applied` (count), `script_ids` (first 50), `confirm_test_set`, `measurement_changed`, `normalizer_version` — never the text, which lives in `corpus_eval_gold_revisions` with both sides of the change. **Severity is `warn` when `measurement_changed > 0`**: most revisions only change how a reference is written and the normalised score does not move, but some change what the subset measures (rewriting the abbreviation gold `АТ` as the spoken `А Те` turns "does the pipeline expand it" into "does the ASR hear it"), and that is a change to the measurement itself. `confirm_test_set` records who authorised editing the frozen holdout |
| `corpus.eval_take_flagged` | info | autocomplete-service `POST /corpus/eval/takes/{script_id}/flag` | a stored take was marked unusable, or the mark was lifted (corpus-v3 Epic E, migration 0096); payload: `script_id`, `condition`, `flagged`. **The only retake signal that is a human judgement** — silence, hallucination and condition mismatch are all derived from dated evidence and clear themselves when the line is re-recorded, so they need no event; "I listened to this and it is unusable" is not derivable from anything and needs a named actor |
| `corpus.speaker_consent_granted` | info | autocomplete-service `POST /compliance/consents` | a speaker consented to their voice being part of the measurement corpus (corpus-v3 Epic F, migration 0097); payload: `speaker_id`, `scope` |
| `corpus.speaker_consent_revoked` | **warn** | autocomplete-service `DELETE /compliance/consents/{speaker_id}` | a speaker withdrew that consent; payload: `speaker_id`, `scope`. **`warn` because it changes what may be measured**: the speaker's takes stop entering NEW snapshots from this moment. Published snapshots are unchanged — the basis that existed when they were frozen is not unmade by a later withdrawal — and no audio is deleted here; erasure is the separate act in the privacy runbook |
| `corpus.data_register_exported` | info | autocomplete-service `GET /compliance/data-register/export.pdf` | the data register was exported for an auditor; payload: `datasets`, `consents`, `format`. Only the PDF export is audited: the JSON and HTML views are ordinary reads of the same console page, while a file leaving the building is the event worth being able to point at |
| `corpus.eval_run_started` | info | autocomplete-service `POST /corpus/eval/runs` | a WER scoring run opened over one SET of a snapshot; payload: `snapshot_version`, `utterances`, `dataset` (`dev`/`test`), `normalizer_version`, `corpus_sha256`. The last three are the reproducibility record (corpus-v2 §1.3.5): a stored WER whose normalisation rules and corpus digest are unknown cannot be compared with any other WER |
| `corpus.eval_run_completed` | info | autocomplete-service `POST /corpus/eval/runs/{id}/advance` (the tick that closes the run) | scoring finished; payload: `status` (`complete`/`failed`), `model`, `utterances_scored`, `wer`, `cer`. The model is in the payload because a WER without its engine is not a measurement — a laptop's `tiny` and the rig's `large-v3` produce numbers that must never be compared |

A PII finding on any of the authoring paths writes
`autocomplete.phrase.write_rejected_pii` (severity `sec`) with the pattern
classes and text lengths — never the matched text.

The candidate *text* is deliberately not in `corpus.candidate_*` payloads:
mined candidates are PHI-derived until reviewed, and audit payloads are
widely readable. The row in `corpus_candidates` (RLS) is the record; the
audit event is the accounting.
