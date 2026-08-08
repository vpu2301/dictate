# EVA-S04 — Sign-off register

## Tech lead

| Artifact | Verification | Status |
|---|---|---|
| `evidence-answer` (:8013): 6-stage traced pipeline, SSE, persistence, provenance | 107 unit tests incl. the golden streaming e2e; `make ci` green | ⏳ |
| `evidence-websearch` (:8014): metasearch, fetcher, extractor, ephemeral index, cache, allowlist admin | 25 unit + 22 security tests | ⏳ |
| Gateway `generator.fast` / `generator.heavy` + client | 15 gateway tests; LM2 clamp echoed into provenance; readiness gating | ⏳ |
| `libs/evidence_safety` (injection screen moved out of ingest + identifier screen) | 11 tests; leaf gate covers it; E2 satisfied without a service-to-service import | ⏳ |
| Contracts v1 additions (stream events, triage decision, `international_organization`) | contracts-check + additive linter green; CHANGELOG entry | ⏳ |
| `web` connector replaces the S03 stub | S03 freeze suite passes **unchanged**; 45 retrieval tests green | ⏳ |
| Migration 0070 (`web_domains`, `web_pages`) | written to 0067/0068 conventions; **cycle + check-rls pending** (as-built delta 8) | ⏳ **open** |
| Prompts v1 + changelog gate | `make check-prompt-changelog`; pins in `answer_provenance.prompt_versions` | ⏳ |

## Security

| Artifact | Verification | Status |
|---|---|---|
| **QS1 layer 1a** — import contract | `make lint-imports`, 7 contracts kept; `allow_indirect_imports` rationale in as-built delta 1 | ⏳ |
| **QS1 layer 1b** — symbol gate | `make check-qs1` over the web path (21 files) | ⏳ |
| **QS1 layer 2** — type wall | `build_query(intent: ClinicalIntent)`; web connector refuses without an intent; fallback intents never reach the web | ⏳ |
| **QS1 layer 3** — runtime taint harness | `make qs1-taint`; **found a real sanitizer gap on first run** (live finding 1); includes a negative control proving the harness fails on a planted leak | ⏳ |
| SSRF defenses | 10 attack fixtures asserted on their specific reason + DNS rebinding, redirect-off-list, oversized body, content-type trap | ⏳ |
| Egress boundary | deny-by-default squid, private ranges blocked, deny log alarmed; **no model-vendor host reachable** (asserted); product allowlist ⊆ network allowlist (asserted) | ⏳ |
| Fail-closed egress | no proxy configured ⇒ client points at a discard port; `EVA_ALLOW_DIRECT_EGRESS` logs a startup WARNING (BE7) | ⏳ |
| LM4 on fetched web text | shared injection screen; a tripping page is dropped outright (no review queue for the open web) | ⏳ |
| Audit payload minimization | deflection payloads carry the reason code, never the question text | ⏳ |
| Audit-not-best-effort | a failed `evidence.answer_generated` write deletes the answer row | ⏳ |
| Dual-key RLS on `questions`/`answers` | `deps.user_connection` sets `app.tenant_id` + `app.user_id`; **live isolation proof pending** (delta 8) | ⏳ **open** |

## Clinical advisor

| Artifact | Verification | Status |
|---|---|---|
| **Triage set v1** (154 items, 77 uk / 77 en) — item labels and the emergency/lookalike split | `docs/eval/triage-set-v1-report.md`; 100% emergency + self-harm recall, 0% false deflection | ⏳ **required** |
| **Safe-messaging text** per reason code, uk + en (incl. the 103 / 7333 referrals) | `domain/stages/triage.py`; rendered verbatim by the SPA | ⏳ **required** |
| **Synthesis prompt v1** — instruction hierarchy, citation discipline, segment grammar, "copy quantities exactly, never round or infer a dose" | `prompts/synthesis_v1.md` | ⏳ **required** |
| **Web trust-tier table + shipped 26-domain allowlist** | `docs/corpus/web-domains.md` | ⏳ **required** |
| Metadata-only designations (paywalled sources cited without body text) | same table; enforced in the connector before any fetch | ⏳ **required** |
| `insufficient_basis` wording, uk + en | `domain/insufficient.py` | ⏳ |
| Curated starter questions (`GET /suggestions`) | `domain/suggestions.py` | ⏳ |
| Generator selection (Gemma 3 4B/12B/27B) — synthesis quality | ADR-0006; measured on the rig, not a laptop | ⏳ **required after the rig run** |

## SRE

| Artifact | Verification | Status |
|---|---|---|
| Degrade paths (web down, corpus down, generator down, extraction garbage, robots skip) | 8 pipeline tests + the fetcher outcome table | ⏳ |
| Readiness semantics: web degradable, generator/retrieval not | `routers/health.py` in both services, with the reasoning inline | ⏳ |
| Concurrency cap (2 in-flight/user, Redis + TTL) | 5 tests incl. refused-slot-does-not-leak and TTL refresh | ⏳ |
| Egress-deny alarm + runbook | `docs/runbooks/evidence-websearch.md`; `TCP_DENIED` grep recipe | ⏳ |
| Runbooks (both services) | symptom→cause tables written from the actual failure modes | ⏳ |
| Observability counters (deflections by reason, fetch outcomes, answers by status, ephemeral index bytes) | in-code counters; **Grafana board not built** (joins the S03 dashboard debt) | ⏳ **open** |
| Latency NFR (first token ≤ 3.5 s warm / ≤ 6 s cold) | **unmeasured** — needs a live llama-server + SearXNG run (as-built delta 9) | ⏳ **open** |
