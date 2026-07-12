# Sprint 10 — Sign-Off

**Sprint dates:** 2026-06-10 → 2026-06-23
**Status:** ✅ Code complete; full ~10k clinical corpus + pilot eyeball pending.

## Scope delivered

- ✅ Day 1: autocomplete-service scaffold + migrations 0023-0025
  (phrases / snippets / telemetry-partitioned) with PERMISSIVE
  `tenant_visibility` + RESTRICTIVE `write_user_phrases`.
- ✅ Day 2: starter system corpus (migration 0026) + corpus CSV/JSON
  README + `scripts/validate-autocomplete-corpus.py`.
- ✅ Day 3: per-tenant trie builder + versioned serializer + Redis
  cache with per-key lock + lazy version_tag invalidation.
- ✅ Day 4: `POST /autocomplete/suggest` (trie + snippet dispatch).
- ✅ Day 5: ranking with Bayesian prior + recency boost + length
  score + Levenshtein diversity guard.
- ✅ Day 6: `POST /autocomplete/telemetry` (fire-and-forget) + PII
  scrubber (6-pattern) + in-memory batch buffer + nightly roll-up
  job + monthly partition rotation.
- ✅ Day 7: phrase create + delete CRUD + PII-rejection on write.
- ✅ Day 8: Grafana sprint-10 dashboard + 5 Prometheus alerts.
- ✅ Day 9: k6 load test + load-test docs.
- ✅ Day 10: ADR-0025 + runbook + architecture doc + PII scrubber
  spec + audit kinds + DPO sign-off template + SIGN-OFF + RETRO.

## Tests

- `autocomplete-service` unit: **33/33 passing**.
- Cumulative sprint 03–10 unit tests: **313 passing**.

## Out of scope

- Layer C generative completion (sprint-15).
- ML-based ranking.
- Cross-language suggestions.
- Per-clinician fine-tuning.
- Embedding-similarity search.
- Real-time collaborative phrase sharing.

## Sign-offs

| role                  | name      | date      |
| --------------------- | --------- | --------- |
| Tech lead             | _pending_ | _pending_ |
| NLP engineer          | _pending_ | _pending_ |
| Clinical content lead | _pending_ | _pending_ |
| Linguist consultant   | _pending_ | _pending_ |
| Security lead         | _pending_ | _pending_ |
| DPO                   | _pending_ | _pending_ |
| Frontend lead         | _pending_ | _pending_ |
| SRE/DevOps            | _pending_ | _pending_ |

## Known follow-ups

1. Clinical content lead authors ~10k UK + ~3k EN phrases + ~60
   snippets per the spec §2.3 corpus shape.
2. Pilot eyeball: 50 anonymised real prefixes rated by clinical
   content lead; target ≥ 80% useful, 0 harmful.
3. DPO formal regex sign-off.
4. Frontend integration: suggest + telemetry + snippet flows.

## Verification addendum (branch S10, 2026-07-07/08)

A step-by-step verification pass against the sprint specs found and
fixed **27 latent defects** — headline items: the learning loop had
never executed end-to-end (partition data loss, roll-up date-type
crash, RLS-no-op'ed counter updates), the phrase write API was
RLS-deny-all (and real multi-role admin tokens were denied even after
that fix), Redis outage 500'd suggest, and **the entire repo's
dashboards/alerts queried metric names the OTel collector never
exported** (namespace prefix; sprints 02–10 affected). Full ledger:
project memory `project_sprint10_verification.md`; fixes shipped as
migrations 0036–0040 + service/infra changes on branch S10.

- Tests now: **autocomplete-service 78 unit + 16 integration = 94**
  (was 33); `make ci` and `make ci-with-db` green.
- Live-proven: learning loop closes (accept → telemetry → roll-up →
  counters → vtag → better ranking), suggest cache-hit p95 4.6 ms
  local, PII rejection + security audit, scope matrix, alert
  firability, dashboard populated.
- Carry-overs (unchanged, tracked in `todo.md` + SPRINT-TODO):
  **~10k corpus authoring — owner: clinical content lead**;
  cold-storage telemetry archival before the 90-day drop —
  **sprint 16**; DPO re-review of the widened phone scrubber pattern.
