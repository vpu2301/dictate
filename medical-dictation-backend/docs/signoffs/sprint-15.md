# Sprint 15 sign-off — Layer C inline completion, Audio replay, Query expansion

Date: 2026-08-02
Branch: `S15` (shares the branch with the separate in-flight patient-contact-details change set — see "Known-dirty" below)
Migrations: `0062_autocomplete_telemetry_source`, `0063_create_medical_synonyms`, `0064_seed_medical_synonyms` (0061 belongs to the in-flight break-glass work; ours renumbered around it). All three proven `down` → `up` on the dev stack.
ADRs: 0036 (Layer C local Gemma), 0037 (audio clip pipeline), 0038 (query expansion)
Pipeline: EXPLORE → PLAN → CREATE

## Premise corrections (EXPLORE)

1. **The "sprint-12 generation-service / Gemma stack" the spec presumed
   does not exist** — sprint 12 shipped notifications. Layer C was built
   greenfield per the owner's decision: local Gemma 3 1B behind an
   `InferenceClient` provider seam, llama-server default (ADR-0036).
2. **`docs/RULES.md` does not exist**; the enforced rules are CLAUDE.md's
   architectural gates. All honored (see gates below).
3. **No GPU rig exists** — the p95 ≤ 400 ms Layer C budget joins the WER/
   DER/streaming-latency gates as the fourth **rig-gated release gate**.

## What shipped

| # | Deliverable | Where |
|---|---|---|
| 1 | Latency measurement + model decision | `scripts/eval/measure_layer_c_latency.py`, PINS.md row, ADR-0036 |
| 2 | generation-service :8009 — `POST /v1/completions/inline` (safety filter, 2-slot pool, dual-window rate limit, tenant flag, silent-204 posture) | `services/generation-service/` |
| 3 | Layer C telemetry on the sprint-10 pipeline (`source='layer_c'`, roll-up isolation, acceptance-rate gauge) | migration 0062, autocomplete-service |
| 4 | Audio replay — segment listing + clip-on-demand + token stream, 410 honesty taxonomy, caps, audits | report-service (`reports_audio.py`, `audio_clips.py`, `domain/audio_{clips,slicer}.py`), ADR-0037 |
| 5 | Query expansion — `medical_synonyms` (RLS, 171 system groups / 464 terms), `expand=false`, `/v1/search/tips`, `/v1/synonyms` CRUD, EXPLAIN regression | migrations 0063/0064, report-service, ADR-0038 |
| 6 | Observability + docs | sprint-15 dashboard, sprint-15-alerts.yml + promtool tests, runbooks, event-kinds, permissions.csv |

## VERIFY — real outputs

### 1. Latency (Apple M5 24 GB, 24-token greedy, ~200-token uk clinical prompt, 50 runs)

| backend | model | mode | p50 | p95 |
|---|---|---|---|---|
| Ollama 0.32.5 | gemma3:1b | alone | 929 ms | 1103 ms |
| Ollama 0.32.5 | gemma3:1b | contended | 1789 ms | 2227 ms |
| llama.cpp b10210 | gemma3:1b | alone | 583 ms | 676 ms |
| llama.cpp b10210 | gemma3:1b | contended | 844 ms | 908 ms |
| Ollama | gemma3:4b | alone | 1690 ms | 2205 ms |

Findings that decided the architecture: Ollama 0.32.5 adds a constant
~420 ms/request scheduler overhead with gemma3 (SWA cache) → llama-server
is the default backend; generation runs 56–73 tok/s on M5, so **the dev
host cannot meet p95 ≤ 400 ms at the spec shape — recorded, not
fabricated** (ADR-0035 posture). Production number = rig-gated gate.

### 2. Inline completion (live HTTP, real Keycloak JWT, real Gemma)

- `{"text_before_cursor":"Пацієнт скаржиться на біль у"}` → **200**
  `{"completion":"плечі, що посилюється при фізичних навантаженнях.",
  "model":"gemma3:1b","latency_ms":952}` (M5; grammatical continuation,
  ≤ 24 tokens).
- Dosage-eliciting fixture («Призначено аспірин у дозі») → **204** and
  the audit chain shows
  `layer_c.completion.filtered | warn | reason=dosage | matched=1000 мг`
  — the model tried to invent a 1000 мг dose and was blocked.
- 40 ms budget twin → **204** (timeout path).
- 12 parallel requests → `429 429 204×10` (burst window holds).
- `/readyz` → `{"status":"ready","layer_c_enabled":true,"model":"gemma3:1b"}`.

### 3. Layer C telemetry

Live POST with PII in the prefix landed as:
`accepted | layer_c | "пацієнт <redacted_PII> скарж" | phrase_id=NULL`.
Integration (`test_rollup_rotation.py`): layer_c rows bump **no** phrase
counters; `layer_c_events_by_type() == {shown_only:3, accepted:1,
rejected:1}`; `layer_c_acceptance_rate() == 0.2`. 4 passed.

### 4. Audio replay (live HTTP + integration)

- Segment listing (conversation fixture): the section's
  `transcript_segment_ids` mapped to exactly the patient turn —
  `{"speaker":"SPEAKER_01","speaker_role":"patient","start_ms":2000,"end_ms":4500}`.
- Clip create → tokenised URL → stream: **HTTP 200, 10 824 bytes,
  ffprobe: `format_name=ogg, duration=3.106500`** = 2500 ms span +
  2×300 ms pad exactly.
- Checksum contract (integration `test_audio_replay_flow.py`): the PCM
  slice equals the independently computed reference slice
  (sha256-equal); whole-object GCM decrypt confirmed as the only read
  path; erased object raises `ObjectNotFoundError`. 1 passed.
- Span 61 000 ms → **422**; 31st clip in the hour → **429**; erased
  audio → **410** `{"code":"audio_erased","detail":"the recording object
  has been deleted (retention/erasure)"}`.
- Every created clip audited: `SELECT count(*) … kind='report.audio_replayed'` → **30**.

### 5. Query expansion

- Integration `test_synonyms_and_expansion.py` (3 passed): «ІМ» finds
  the report containing only «інфаркт міокарда» **and vice versa**;
  `expand=false` does not; snippets keep `<mark>` under the expanded
  tsquery; tenant-B rows invisible to A; app_role cannot INSERT/DELETE
  system rows (42501 / DELETE 0).
- Live HTTP: `?q=ІМ` → `expanded_terms: ["інфаркт міокарда","ГІМ",
  "гострий інфаркт міокарда","MI","myocardial infarction"]`;
  `expand=false` → `[]`; tips endpoint serves uk/en with the
  no-stemming honesty copy; clinician POST /v1/synonyms → **403**,
  tenant_admin → **201** and DELETE → **204**.
- **EXPLAIN (pasted in the test output)**: the expanded tsquery hits
  `Bitmap Index Scan on report_versions_search_vector_idx` (index-path
  proof), and the app_role/RLS plan keeps the tenant-first Nested Loop.
  **Discovery**: `@@` is not leakproof ⇒ under RLS the GIN index was
  NEVER used by app_role — sprint-08's "canonical GIN plan" was captured
  as superuser. Recorded in ADR-0038 with the escalation lever.
- Adversarial cap: unit `test_expansion_cap_holds_on_adversarial_query`
  proves exactly 8 groups expand, overflow lexemes stay plain.

### 6. Defect found & fixed by this sprint's own tests

`fetch_matching_synonyms` originally returned only the rows overlapping
the query lexemes — never the group siblings — so expansion silently
expanded to nothing. Caught by the live-DB integration test (the unit
tests fed full groups by hand); fixed with the group-membership subquery
and locked by the integration assertions.

## Gates

- `make lint` ✔ · `make typecheck` ✔ · `make lint-imports` (19 contracts,
  incl. new generation-service layering) ✔ · `make security` (bandit HIGH:
  0, exit 0) ✔ · `make check-alert-rules` (sprint-15 promtool tests) ✔ ·
  `check-no-{os-environ,direct-asyncpg,object-storage,crypto}` ✔ ·
  `check-audit-insert` ✔ · `check-notification-phi-free` ✔ ·
  `validate-templates` ✔ · `check-autocomplete-corpus` ✔ ·
  `make check-synonym-corpus` (new gate, fixture↔migration sync) ✔ ·
  `make check-rls` — **36 tables RLS+FORCE incl. medical_synonyms** ✔ ·
  `make check-erasure-fanout` ✔ (clips deliberately add no PHI table) ·
  `make openapi-dump` (generation-service snapshot added;
  report/autocomplete refreshed; openapi-check green post-commit) ✔
- `make test`: all packages green **except two pre-existing failure
  clusters proven present with S15 changes stashed**: (a)
  `libs/template_models::test_all_seed_templates_validate_and_dump_byte_identical`
  (cardiology_outpatient_en.json fixture drift — the S13 `required`
  regression trail), (b) `services/signing-service` 14 failures in
  test_local_upload/related (403-vs-404 assertion drift). Neither is
  touched by this sprint.
- Migrations `0062–0064` applied, rolled back ×3, re-applied on the live
  stack ✔ (`make migrate-status` clean).

## Known-dirty (in-flight patient-contact work sharing this branch — NOT this sprint's)

- untracked `0060_patient_contact_details` + `0061_phi_access_patient_kind`
  migrations, modified core-service patient files, regenerated
  `docs/api/core-service-openapi.json`, root `README.md` edits. Our
  migrations were renumbered 0062+ around their 0061 mid-sprint.
- One mechanical touch: removed an unused import their linter flagged in
  `core-service/tests/unit/test_patient_break_glass.py` to unblock
  `make lint`.

## Carry-overs (named, not dropped)

- **GPU rig**: Layer C p95 ≤ 400 ms production measurement + the Gemma
  image bake (PINS.md pin recorded) — joins WER/DER/latency on the rig
  backlog.
- **Clinical-lead review** of the 171 seeded synonym groups (fixture is
  flagged in-file).
- `transcript_segment_ids` population for non-conversation report flows
  (listing falls back to the whole session transcript meanwhile).
- RLS + GIN: if search p95 breaches ADR-0021 triggers, the leakproof
  finding names the fix (SECURITY DEFINER search path).

---

# Sprint 15 — Deployment sign-off (clips bucket, model residency, edge limit)

Date: 2026-08-02 · Pipeline: EXPLORE → PLAN → CREATE (deployment spec)

## EXPLORE verdicts

1. **MinIO lifecycle support** — confirmed and already exercised twice
   (mdx-dsar 7 d, mdx-backups 35 d, both from S11/ADR-0028). The S15
   backend slice reused the pattern in minio-init:
   `mc ilm rule add --expire-days 1 local/mdx-audio-clips`
   (1 day is mc's floor — sub-day is impossible; the REAL 5-min clip
   lifetime is the Redis `SETEX 300` registry, ADR-0037; ILM is the
   ciphertext backstop).
2. **Model residency verdict: shared single model — the fallback is NOT
   triggered.** ADR-0036's measurement already selected the *smallest*
   Gemma (3 1B Q4_K_M; 4B was measured and rejected at 1690/2205 ms) —
   there is no "smaller completion model" below the one in service, so
   no second-model bake, readiness change, or VRAM re-threshold applies.
   The bake/pin flow itself stays rig-deferred (no GPU rig exists;
   PINS.md records the pin, ADR-0036 records the rig-gated p95 gate).
   The conditional CREATE item is therefore closed as **not applicable
   by measurement**, not skipped.

## CREATE 1 — mdx-audio-clips: encrypted, expiring, never backed up

Bucket + ILM live (dev stack, 2026-08-02):

```
$ mc ilm rule ls local/mdx-audio-clips
│ ID                   │ STATUS  │ PREFIX │ TAGS │ DAYS TO EXPIRE │ EXPIRE DELETEMARKER │
│ d9nkpdi847rs67vemc6g │ Enabled │ -      │ -    │              1 │ false               │
```

Better than a synthetic probe: the encrypted clip objects created by
this sprint's own replay verification are each stamped with the expiry
the rule assigns:

```
Name      : clips/00000000-…-000a/10af6194-77db-4a22-8e78-e7790e473523.ogg.enc
Size      : 3.4 KiB
Expiration: 2026-08-04 00:00:00 UTC (lifecycle-rule-id: d9nkpdi847rs67vemc6g)
```

(A 50-byte probe object was also uploaded and carries the same rule.)

**Backup exclusion, provable**: the backup job's entire upload set is
the pg_dump artifact pair —

```
$ grep -n "mc cp\|mc mirror" deploy/scripts/backup.sh
76:    mc cp /backup/$BACKUP_ID.dump.enc local/$BUCKET/      # BUCKET="mdx-backups"
77:    mc cp /backup/$BACKUP_ID.manifest.json local/$BUCKET/
$ grep -rn "mdx-audio-clips" deploy/   # only the policy-exclusion docs
```

No bucket is mirrored into backups anywhere in deploy/. The exclusion
is now **recorded as policy** (not an accident of scope) in
`deploy/scripts/backup.sh` header + `deploy/README.md`: clips are
regenerable derivatives and must never outlive their source audio in a
backup. Encryption + crypto-shredding on erasure come free from the
tenant-KEK envelope (AAD = clip_id, ADR-0037).

## CREATE 2 — second-model image: not applicable (see EXPLORE 2)

No infra change. Readiness stays single-model
(`/readyz → {"status":"ready","model":"gemma3:1b"}` — verified in the
backend sign-off); Layer C alerts stay as shipped in
sprint-15-alerts.yml.

## CREATE 3 — edge rate limit on the completion path

`public-edge` allowlist grows to exactly **three** public paths
(infra/edge/nginx.conf.template): the Layer C typing path
`POST /v1/completions/inline` is proxied to generation-service behind a
new per-IP zone — `rate=5r/s, burst=8, nodelay` — deliberately BELOW
the app-layer per-user burst (10/s) so a flood dies at the edge without
spending app CPU, and comfortably above debounced typing (~1–2 r/s).
`limit_except POST` + `client_max_body_size 64k` (a typing prefix,
never a document). Compose: `GENERATION_UPSTREAM=generation-service:8000`
+ health-gated depends_on.

**VERIFY (live against https://localhost:8443, real Keycloak JWT
minted in-network, real Gemma inference behind the edge):**

- Allowlist still holds: `GET /v1/reports` → **404**, `GET /healthz` →
  **404**; method guard: `GET /v1/completions/inline` → **403**.
- **Normal typing cadence never trips**: 12 requests @ 0.5 s →
  `200 ×12`, e.g. `{"completion":"плечі, що посилюється при фізичних
  навантаженнях.","model":"gemma3:1b","latency_ms":335}`.
- **Unauthenticated flood**: 40 back-to-back → first 8 pass the burst
  (reach the service, die as 401 at its JWT layer), the rest **429
  served by nginx** (`server: nginx/1.27.5`, HTML error page — not the
  app's JSON).
- **Authenticated parallel flood (edge trips BEFORE app)**: 40
  concurrent with a valid JWT → `30 × 429` (edge zone), `10 × 204`
  (the app's slot-pool silence posture) — **zero app-layer 429s**
  (the app limiter emits JSON + Retry-After; none observed), i.e. the
  edge cap absorbed the flood below the app's own windows. The next
  single request answered **200** — instant recovery.
- Sequential authenticated hammering never trips the edge at all
  (real inference latency self-throttles one connection to < 5 r/s) —
  the cap only bites genuinely concurrent abuse.

## Definition of Done — deployment

- Clips: ephemeral (Redis 300 s + ILM 1 d backstop) ✔ · encrypted
  (tenant-KEK envelope) ✔ · unbacked-up **by recorded policy** ✔ ·
  expiring (rule live, objects stamped) ✔
- Model residency matches the measured decision (single gemma3:1b;
  fallback n/a by measurement; bake rig-deferred) ✔
- Edge limit: trips before the app limit under flood, invisible at
  typing cadence ✔ — real outputs above.
