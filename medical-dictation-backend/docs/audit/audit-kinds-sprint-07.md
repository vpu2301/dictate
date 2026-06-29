# Audit kinds — Sprint 07 additions

Appends to the canonical list in `docs/audit/audit-kinds.md`. The
sprint-02 audit verifier consults the merged set; anything else is
rejected.

| kind                          | emitter             | payload keys                         |
| ----------------------------- | ------------------- | ------------------------------------ |
| `demo.rate_limit_hit`         | auth-service        | `ip`, `axis`, `detail`               |
| `demo.session_capped`         | dictation-service   | `session_id`, `minutes_used`         |
| `demo.daily_minutes_capped`   | dictation-service   | `user_id`, `minutes_used`            |
| `demo.ip_blocked`             | auth-service        | `ip`, `cooldown_seconds`             |
| `demo.privacy_test_passed`    | daily privacy CI    | `run_id`, `checks` (array)           |
| `demo.privacy_test_failed`    | daily privacy CI    | `run_id`, `residual_files` (array)   |
| `eval.run.started`            | `run_wer.py`        | `corpus_version`, `utterances`       |
| `eval.run.completed`          | `run_wer.py`        | `run_id`, `wer_uk`, `wer_en`         |
| `eval.run.regressed`          | `compare_to_baseline.py` | `run_id`, breached thresholds  |

Demo kinds are part of `libs/demo/src/demo/audit_kinds.py`
(`DEMO_AUDIT_KINDS` frozenset). The runtime audit verifier
short-circuits the closed-set check only when `MDX_DEMO_MODE=true` —
production deployments do not accept these kinds.

Eval kinds are part of `scripts/eval/audit_kinds.py` (`EVAL_AUDIT_KINDS`
frozenset). The eval pipeline is a non-tenant, system-level CI job, so
these are surfaced as structured log events + Prometheus gauges + Slack
alerts (`#eval-regressions`) rather than hash-chained `audit.events`
rows.
