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
| `auth.account_locked`             | sec      | auth-service /auth/login         | Keycloak rejected the login with account-locked detail (currently surfaced as HTTP 423 + structured log; audit-row TBD) |
| `authz.denied`                    | sec      | auth-service `requires()` dep    | Role/scope check failed. Payload carries action + target_kind + reason. |
| `user.invited`                    | info     | auth-service /admin/users/invite | tenant_admin created a new user                                    |
| `user.deactivated`                | sec      | auth-service /admin/users/{sub}/deactivate | Sessions revoked, status flipped                         |
| `user.reactivated`                | sec      | auth-service /admin/users/{sub}/reactivate | Deactivated user re-enabled; status flipped back to active |
| `user.role_changed`               | sec      | auth-service PUT /admin/users/{sub}/roles | Realm roles changed by tenant_admin. Payload carries old_roles → new_roles. |
| `user.reset_mfa`                  | sec      | *(sprint 16+)*                   | MFA enrolment cleared by admin                                     |
| `audit.chain_verified`            | info/sec | nightly verifier                 | One per tenant per verify run. severity flips to `sec` on divergence |
| `asr.audio_uploaded`              | info     | asr-service POST /asr/jobs       | Audio file enveloped + persisted; row inserted in `audio_files`    |
| `asr.audio_deleted`               | sec      | *(sprint 11)*                    | Right-to-erasure delete of an audio object                         |
| `asr.job_queued`                  | info     | asr-service POST /asr/jobs       | Job durably recorded + enqueued on Redis Streams                   |
| `asr.transcription_started`       | info     | asr-worker processor             | Worker picked the job up; row moved to `running`                   |
| `asr.transcription_complete`      | info     | asr-worker processor             | Inference + encrypted transcript stored; row moved to `complete`   |
| `asr.transcription_failed`        | error    | asr-worker processor             | Job failed with `error_kind` (gpu_oom, corrupt_audio, timeout, …)  |
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
| `report.pdf_rendered`             | info     | report-service GET /v1/reports/{id}/pdf | M1 — unsigned PDF rendered for local KEP. Payload: version_number, size_bytes, purpose |
| `report.completed`                | info     | report-service POST /v1/reports/{id}/finalize | M1 — finalize completion summary (paired with `report.finalized`). Payload: version_number, section_count, low_confidence_count, source_session_id |
| `signing.session.cancelled`       | info     | signing-service DELETE /signing/sessions/{id} | M1 — user aborted an in-flight session. Payload: from_status |
| `signing.session.local_upload`    | info     | signing-service POST /signing/sessions/{id}/upload | M1 — locally-signed PAdES uploaded + verified (paired with `signing.envelope.persisted`). Payload: provider, signed_envelope_id, is_qualified |
| `report.synthesis_started`        | info     | report-service POST /v1/reports/{id}/synthesize | Spec item 1 — synthesis run begun. Payload: section_count, language, provider |
| `report.synthesis_completed`      | info     | report-service POST /v1/reports/{id}/synthesize | Spec item 1 — synthesis run finished (paired with `report.synthesis_started`). Payload: job_id, section_count, language, provider |
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

## Adding a new kind

1. Define the constant in `services/<service>/src/<service>/audit_kinds.py`.
2. Use it via `await audit_writer.write_event(kind=audit_kinds.X, ...)`.
3. Add a row to this table.
4. If the kind warrants its own dashboard panel or alert rule, add
   them in `infra/grafana/dashboards/` and `infra/prometheus/rules/`.

## Payload conventions

The `payload` arg to `write_event` is the caller-supplied dict that lands
*inside* the canonicalised event record under the `payload` key. Keep it
shallow (no deeply nested objects) and pre-convert non-JSON types
(UUID → str, datetime → ISO-8601). The writer's `_normalize_payload`
handles UUID/datetime/bytes for you.

Sensitive values (passwords, raw OTP codes, PHI) **must not** appear in
the payload. Audit is for *who did what when* — the *what* references
IDs, not contents.
