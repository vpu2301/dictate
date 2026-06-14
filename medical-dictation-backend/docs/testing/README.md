# Testing the system from the terminal

A hands-on walkthrough: every command you run to bring the platform up and
exercise it end to end, paired with the response you should expect. Copy-paste
friendly. All commands run from the repo's **`medical-dictation-backend/`**
directory unless noted.

There are two ways to drive everything below:

- **Raw commands** — `make` targets and `curl` calls (this document).
- **The `mdx-test` CLI** — one wrapper with coloured PASS/FAIL output:
  `bash scripts/dev/mdx-test.sh <command>` (see [§8](#8-the-mdx-test-cli)).

> Conventions: `$` is your shell prompt. Responses are abbreviated with `…`.
> Everything targets the **local dev stack** — nothing here touches prod.

---

## 0. Prerequisites

```bash
$ make doctor
```

Expected — every line a green `✓`:

```
✓ docker (28.x) running
✓ python 3.12.7
✓ uv 0.4.x
✓ make, git, curl, jq
All checks passed.
```

If a line is red, fix it before continuing (`make doctor` prints the remedy).
You also want `ffmpeg` and `jq` for the ASR / Keycloak flows:

```bash
$ brew install ffmpeg jq      # macOS
```

---

## 1. Bring the stack up

```bash
$ make dev-up        # Postgres, Redis, MinIO, Kafka, Keycloak, OTel/Jaeger/Prom/Grafana/Loki
$ make migrate-up    # apply SQL migrations
$ make seed          # dev tenants + users + sample data
```

Expected:

```
# dev-up
[+] Running 11/11
 ✔ Container mdx-postgres   Healthy
 ✔ Container mdx-keycloak   Healthy      # first boot ~30s — be patient
 …
# migrate-up
applied 0001_init.sql … 00NN_*.sql  (NN migrations, 0 pending)
# seed
seeded tenants: tenant-a, tenant-b · users: 3 · prompts/templates/abbrev: ok
```

> **Only infra runs in compose.** The feature services you start yourself
> (uvicorn or the compose dev overlay) — see [§4](#4-start-the-services).

Verify the infra is healthy:

```bash
$ make smoke
```

Expected:

```
── Infrastructure ──
✓ Jaeger UI
✓ Prometheus
✓ Grafana
✓ Loki ready
Results: 4 passed, 0 failed
```

Or with the CLI: `bash scripts/dev/mdx-test.sh infra`.

---

## 2. Infrastructure health (manual curls)

| What | Command | Expected |
| ---- | ------- | -------- |
| Postgres | `pg_isready -h localhost -p 5432` | `localhost:5432 - accepting connections` |
| Redis | `redis-cli -h localhost ping` | `PONG` |
| MinIO | `curl -s localhost:9000/minio/health/live -o /dev/null -w '%{http_code}\n'` | `200` |
| Keycloak | `curl -s localhost:8088/realms/medical-dictation/.well-known/openid-configuration \| jq .issuer` | `"http://localhost:8088/realms/medical-dictation"` |
| Prometheus | `curl -s localhost:9090/-/healthy` | `Prometheus Server is Healthy.` |
| Grafana | `curl -s localhost:3000/api/health \| jq .database` | `"ok"` |
| Jaeger | open `http://localhost:16686` | trace UI loads |

---

## 3. Authentication

The platform authenticates against Keycloak (realm `medical-dictation`). All
protected endpoints want a `Bearer` access token.

### 3a. Get a token (direct grant)

```bash
$ curl -s -X POST \
    -d grant_type=password -d client_id=mdx-dev-cli \
    -d username=dev-clinician -d password=dev-password -d scope=openid \
    http://localhost:8088/realms/medical-dictation/protocol/openid-connect/token \
  | jq -r .access_token
```

Expected — a JWT (`eyJ…`). Decode the claims to confirm `tid` (tenant) and
`roles`:

```bash
$ TOKEN=$(... the curl above ...)
$ echo "$TOKEN" | cut -d. -f2 | base64 -d 2>/dev/null | jq '{sub,tid,roles,aud,iss}'
```

```json
{
  "sub": "…uuid…",
  "tid": "…tenant-uuid…",
  "roles": ["clinician"],
  "aud": ["mdx-api", "mdx-backend"],
  "iss": "http://localhost:8088/realms/medical-dictation"
}
```

Shortcut: `bash scripts/dev/mdx-test.sh token` prints the token **and** the
decoded claims.

### 3b. Full IdP smoke (login → introspect → refresh → replay-reject)

```bash
$ make keycloak-test
```

Expected — the last line proves refresh-token rotation + replay detection:

```
PASS: JWKS reachable (kid=…)
PASS: Login succeeded for dev-clinician
PASS: Access token has all expected claims (sub, tid, roles, iss, aud, exp, iat, sid)
PASS: Token introspect: active
PASS: Refresh succeeded and refresh_token rotated
PASS: Old refresh_token correctly rejected on replay (HTTP 400)
All Keycloak smoke checks passed.
```

### 3c. Through auth-service (what the SPA uses)

`auth-service` proxies login and sets a SameSite cookie. Start it
(`make run-auth-service`, see §4) then:

```bash
$ curl -s -X POST http://localhost:8000/auth/login \
    -H 'Content-Type: application/json' \
    -d '{"email":"dev-clinician","password":"dev-password"}' | jq
```

```json
{ "access_token": "eyJ…", "token_type": "Bearer", "expires_in": 300 }
```

```bash
$ curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/auth/me | jq
```

```json
{ "sub": "…", "tid": "…", "roles": ["clinician"], "email": "dev-clinician", "db_user": {…} }
```

CLI equivalent: `bash scripts/dev/mdx-test.sh auth`.

---

## 4. Start the services

Each service is a FastAPI app on container port `8000`. The dev compose overlay
maps four of them; the rest you run with uvicorn.

| Service | How to start | Host URL |
| ------- | ------------ | -------- |
| auth-service | `make run-auth-service` | `http://localhost:8000` |
| asr-service | `make dev-up-asr` (CPU overlay) | `http://localhost:8001` |
| dictation-service | `make dev-up-asr` | `http://localhost:8002` |
| nlp-service | `make dev-up-asr` | `http://localhost:8005` |
| report-service | `make dev-up-asr` | `http://localhost:8006` |
| signing-service | `uv run --project services/signing-service uvicorn signing_service.main:app --port 8007` | `http://localhost:8007` |
| autocomplete-service | `uv run --project services/autocomplete-service uvicorn autocomplete_service.main:app --port 8008` | `http://localhost:8008` |

> The host ports `8001/8002/8005/8006` come from `infra/compose/dev.yml`.
> `signing`/`autocomplete` aren't in compose — the ports above are this doc's
> convention; pass `--port` whatever you like and set the matching env override
> for the CLI.

Health-check whatever you started:

```bash
$ curl -s localhost:8001/healthz | jq      # → {"status":"ok"}
$ curl -s localhost:8001/readyz  | jq      # → {"status":"ready", "checks":{…}}
```

Expected `/healthz` (liveness, no deps): `{"status":"ok"}`.
Expected `/readyz` (readiness, checks pools): `{"status":"ready", …}` — a `503`
with `"status":"not_ready"` means a dependency (DB/Redis/MinIO) is down.

CLI: `bash scripts/dev/mdx-test.sh health` checks every service at once and
**skips** (∅, not ✗) the ones you haven't started.

---

## 5. End-to-end service flows

### 5a. Batch ASR (asr-service → asr-worker)

Submits a 1-second silent WAV, then polls the job to completion. Needs a prompt
id from the seeded `medical_prompts` table:

```bash
$ PROMPT_ID=$(docker exec -i "$(docker ps -qf name=postgres)" \
    psql -U postgres -d medical_dictation -tAc \
    "select id from medical_prompts where language='uk' limit 1" | tr -d '[:space:]')
$ ASR_SERVICE_URL=http://localhost:8001 PROMPT_ID=$PROMPT_ID \
    bash scripts/dev/asr-smoke.sh
```

Expected:

```
queued job_id=…uuid…
…uuid… → queued
…uuid… → processing
…uuid… → complete
ok
```

CLI: `PROMPT_ID=$PROMPT_ID bash scripts/dev/mdx-test.sh asr`.

The raw submit, if you want to drive it by hand:

```bash
$ ffmpeg -loglevel error -f lavfi -i anullsrc=r=16000:cl=mono -t 1 -c:a pcm_s16le /tmp/s.wav
$ curl -s -X POST http://localhost:8001/asr/jobs \
    -H "Authorization: Bearer $TOKEN" \
    -F 'audio=@/tmp/s.wav;type=audio/wav' -F language=uk -F "prompt_id=$PROMPT_ID" | jq
# → {"id":"…","status":"queued", …}
$ curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8001/asr/jobs/<id> | jq .status
# → "complete"
```

### 5b. NLP pipeline (nlp-service)

Runs the 6-stage pipeline (voice commands → punctuation → numbers → dates →
abbreviations → confidence) on one segment:

```bash
$ curl -s -X POST http://localhost:8005/nlp/process \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d '{"text":"тиск сто двадцять на вісімдесят крапка новий рядок","language":"uk","words":[]}' | jq
```

Expected — numbers normalised, the `крапка` / `новий рядок` voice commands
applied:

```json
{
  "text": "Тиск 120/80.\n",
  "commands": [{"intent":"punctuation","op":"period"}, {"intent":"newline"}],
  "confidence": 0.9,
  "pipeline_version": "…"
}
```

CLI: `bash scripts/dev/mdx-test.sh nlp`.

### 5c. Reports (report-service)

```bash
# list system templates
$ curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8006/templates | jq '.[0].name'
# → "Загальний огляд"  (one of 16 system templates)

# create a draft report
$ curl -s -X POST http://localhost:8006/v1/reports \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d '{"template_id":"<uuid>","title":"Test report","sections":{}}' | jq '{id,status,version}'
# → {"id":"…","status":"draft","version":1}

# autosave (optimistic lock), then finalize
$ curl -s -X PUT  http://localhost:8006/v1/reports/<id> -H "Authorization: Bearer $TOKEN" \
    -H 'Content-Type: application/json' -d '{"sections":{"impression":"ok"},"base_version":1}' | jq .version
# → 2
$ curl -s -X POST http://localhost:8006/v1/reports/<id>/finalize -H "Authorization: Bearer $TOKEN" | jq .status
# → "final"

# full-text search + version diff
$ curl -s -H "Authorization: Bearer $TOKEN" "http://localhost:8006/v1/reports/search?q=test" | jq length
$ curl -s -H "Authorization: Bearer $TOKEN" "http://localhost:8006/v1/reports/<id>/diff?from=1&to=2" | jq
```

### 5d. Streaming dictation (dictation-service, WebSocket)

Protocol `medical-dictation.v1` over `ws://localhost:8002/ws/dictate`. Quick
reachability check (a plain GET on a WS route returns `426 Upgrade Required` —
that means the route is alive):

```bash
$ curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8002/dictate/ws/dictate
# → 426
```

For a real session use `websocat` (send the `start` frame with your token, then
binary Opus frames). See `docs/architecture/` and the dictation client fixtures
under `tests/` for the exact frame sequence.

### 5e. Autocomplete (autocomplete-service)

```bash
$ curl -s -X POST http://localhost:8008/autocomplete/suggest \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d '{"prefix":"паці","language":"uk","limit":5}' | jq
# → {"suggestions":[{"phrase":"пацієнт скаржиться на…","score":…}, …]}
```

CLI: `bash scripts/dev/mdx-test.sh autocomplete`. Target p95 ≤ 80 ms.

### 5f. Signing + public verify (signing-service)

```bash
# create a signing session for a finalized report (mock КЕП provider in dev)
$ curl -s -X POST http://localhost:8007/signing/sessions \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d '{"report_id":"<uuid>","provider":"mock"}' | jq '{session_id,verify_token}'

# public verify — NO auth required (this is the citizen-facing endpoint)
$ curl -s http://localhost:8007/verify/<verify_token> | jq '{valid,signer,signed_at}'
# → {"valid":true,"signer":"…","signed_at":"…"}

# unknown token → 404
$ curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8007/verify/deadbeef
# → 404
```

---

## 6. Tenant isolation (RLS) spot-check

Multi-tenancy is enforced in Postgres, not app code. A token for tenant-A must
never see tenant-B rows. Grab two tokens and confirm the report lists differ:

```bash
$ A=$(USERNAME=dev-clinician PASSWORD=dev-password bash scripts/dev/mdx-test.sh token | head -1)
# (tenant-B admin / a second seeded user gives the B token — see dev creds below)
$ curl -s -H "Authorization: Bearer $A" http://localhost:8006/v1/reports | jq 'length'
```

Expected: a request scoped to tenant-A returns only tenant-A reports; reusing a
tenant-A token to fetch a tenant-B report id returns `404` (RLS hides the row,
it is not a `403`).

---

## 7. Quality gates (no running stack needed)

```bash
$ make lint          # ruff           → "All checks passed!"
$ make typecheck     # mypy --strict  → "Success: no issues found"
$ make test          # pytest         → "N passed" per package
$ make security      # bandit+pip-audit+semgrep → no high findings
$ make lint-imports  # import-linter  → "Contracts: N kept, 0 broken"
$ make ci            # all blocking gates together
```

With the stack up you can also run the DB-backed gates:

```bash
$ make ci-with-db    # ci + check-rls + openapi-check  (needs dev-up + migrate-up)
```

Expected `check-rls`: `✓ every user-schema table has RLS + FORCE`.

---

## 8. The `mdx-test` CLI

A single wrapper around all of the above with coloured PASS / FAIL / SKIP output.

```bash
$ bash scripts/dev/mdx-test.sh help
```

| Command | Does |
| ------- | ---- |
| `doctor` | `make doctor` |
| `up` | `dev-up` + `migrate-up` + `seed` |
| `infra` | health-check Jaeger/Prometheus/Grafana/Loki/MinIO |
| `health` | health-check every service `/healthz` + `/readyz` (skips ones not started) |
| `all` | `infra` + `health` + token check |
| `token` | fetch an access token and decode its claims |
| `keycloak` | full login→introspect→refresh→replay flow |
| `auth` | auth-service `/auth/login` then `/auth/me` |
| `asr` | batch ASR smoke (needs `ffmpeg` + `PROMPT_ID`) |
| `nlp` | run the NLP pipeline on one segment |
| `autocomplete` | autocomplete suggest |
| `signing` | public `/verify` reachability |

Output legend: `✓` pass · `✗` fail · `∅` skipped (service not reachable / not
started). The CLI exits non-zero if anything **fails** (skips don't fail it),
so it's safe in scripts.

Override any endpoint or credential via env vars, e.g. point at a service you
started on a different port:

```bash
$ SIGNING_URL=http://localhost:9100 USERNAME=dev-admin PASSWORD=dev-password \
    bash scripts/dev/mdx-test.sh all
```

Recognised overrides: `KEYCLOAK_URL AUTH_URL ASR_URL DICTATION_URL NLP_URL
REPORT_URL SIGNING_URL AUTOCOMPLETE_URL USERNAME PASSWORD REALM CLIENT_ID
PROMPT_ID`.

---

## 9. Dev credentials (local only)

| Who | Username / email | Password |
| --- | ---------------- | -------- |
| Keycloak admin console | `admin` | `admin` (`http://localhost:8088`) |
| CLI direct-grant user | `dev-clinician` | `dev-password` |
| Tenant-A admin | `admin@tenant-a.example` | `dev-password` |
| Tenant-B admin | `admin@tenant-b.example` | `dev-password` |
| Clinician | `clinician@tenant-a.example` | `dev-password` |

Infra creds (`postgres/postgres`, `minioadmin/minioadmin`, Grafana `admin/admin`)
are in the root `README.md` service-URL table.

> The `keycloak-test.sh` / `mdx-test.sh` default user is `dev-clinician`
> (client `mdx-dev-cli`). The `asr-smoke.sh` default is `clinician@a.test`.
> If a login 401s, list realm users in the Keycloak admin console and adjust
> `USERNAME` / `PASSWORD`.

---

## 10. Tear down

```bash
$ make dev-down      # stop + remove containers (volumes kept)
$ make reset-db      # wipe & recreate the Postgres volume (also re-imports Keycloak realm)
```

> Don't drop the Postgres volume by hand — Keycloak shares that server, and an
> orphaned realm breaks auth. Use `make reset-db`.
