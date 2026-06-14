# Sprint 00 — Monorepo Bootstrap & Platform Foundations
## Canonical Specification (CTO-Level) — *reconstructed as-built*

**Type:** Engineering specification — architecture, work plan, verification protocol, risk register
**Owner:** Backend tech lead
**Reviewers:** every backend engineer, security lead, SRE/DevOps lead
**Status:** Accepted (reconstructed from the as-built repo; canonical text was originally in conversation history)
**Depends on:** nothing — this is the ground floor
**Hands off to:** sprint-01 (service template hardening), sprint-02 (Keycloak auth), sprint-03 (first ML path)

> **Reconstruction note.** This document is rebuilt from the shipped
> artifacts (ADR-0001…0005, `pyproject.toml`/`uv.lock`, `libs/`,
> `services/_template`, `docs/onboarding.md`, `Makefile`, CI workflows).
> It is the in-repo canonical record; where the original day-by-day
> differed, the as-built code is authoritative.

---

## 0. Why this sprint exists

A medical-dictation platform handling PHI cannot be retrofitted for
security, isolation, and observability later. Sprint-00 lays a
foundation where **every** future service inherits — by construction —
tenant isolation, secret hygiene, PII-safe logging, deterministic
builds, and a green CI gate. The deliverable is not a feature; it is a
**paved road**: a service template plus shared libraries so that
sprint-03's ASR worker and sprint-12's generation service look and
behave identically at the infrastructure layer.

The guiding principle: *make the secure path the easy path.* If an
engineer has to think to be safe, the platform has already failed.

---

## 1. Scope

### 1.1 In scope

1. **`uv` workspace monorepo** (ADR-0001): Python 3.12 pinned via
   `.python-version`; root `pyproject.toml` declares the workspace;
   each service/lib carries its own `pyproject.toml`; one `uv.lock`.
2. **Shared libraries** (`libs/`), each independently importable:
   - `libs/secret` — typed `Secret[T]` wrapper (ADR-0003).
   - `libs/db` — `tenant_connection` RLS helper (ADR-0004).
   - `libs/observability` — OTel bootstrap + `PIISafeFilter`
     (ADR-0005).
3. **Service template** `services/_template/` — the canonical shape
   every new service is copied from: FastAPI factory, `/healthz` +
   `/readyz`, `RequestIDMiddleware`, exception handlers, config via
   pydantic-settings, OTel instrumentation.
4. **Production container standard** (ADR-0002): distroless, nonroot,
   multi-stage build.
5. **Local dev stack** via Docker Compose: Postgres, Redis, MinIO,
   Kafka, Keycloak, and the OTel→(Tempo/Jaeger)+Loki+Prometheus+Grafana
   observability plane. `make dev-up` / `make smoke`.
6. **CI gate**: `lint` (ruff), `typecheck` (mypy --strict), `test`
   (pytest), `security`, `import-linter`, `container-scan` (trivy,
   CRITICAL/HIGH fail). Green-or-no-merge.
7. **Developer ergonomics**: `make doctor`, `make ci` (mirrors CI),
   `docs/onboarding.md` (clean machine → green run in 30 min),
   `docs/adr/` with the authoring template + README index.
8. **Config discipline**: env reads live **only** in a service's
   `config.py` pydantic settings; a pre-commit hook rejects stray
   `os.environ` reads.

### 1.2 NOT in scope — see §9.

---

## 2. Architecture & contracts

### 2.1 Workspace layout (the contract every sprint inherits)

```
medical-dictation-backend/
├── pyproject.toml          # uv workspace root
├── uv.lock                 # single lockfile, deterministic
├── .python-version         # 3.12 patch pin
├── Makefile                # doctor / dev-up / smoke / ci / test
├── libs/
│   ├── secret/             # Secret[T]                (ADR-0003)
│   ├── db/                 # tenant_connection        (ADR-0004)
│   └── observability/      # OTel + PIISafeFilter     (ADR-0005)
├── services/
│   └── _template/          # copy-me service skeleton
├── infra/
│   ├── postgres/migrations/
│   ├── grafana/dashboards/
│   └── compose/            # dev stack
└── docs/
    ├── adr/                # 0001…0005 + README + template
    ├── onboarding.md
    └── glossary.md
```

**Rule:** a new service is `cp -r services/_template services/<name>`
and nothing about health, config, logging, tracing, or error handling
is hand-written again. This is what makes sprint-03/04/06/08/10/12 look
uniform.

### 2.2 `Secret[T]` (ADR-0003)

A typed wrapper that holds a sensitive value, prints as `***` in
`repr`/`str`/logs/tracebacks, and only yields the inner value through an
explicit `.reveal()` call. Configuration models type their sensitive
fields as `Secret[str]` so a stray log line or exception can never leak
a DB password or signing key.

```python
class Secret(Generic[T]):
    def __init__(self, value: T) -> None: ...
    def reveal(self) -> T: ...
    def __repr__(self) -> str: return "Secret(***)"
    __str__ = __repr__
```

### 2.3 `tenant_connection` (ADR-0004) — the isolation primitive

The single helper through which **all** tenant-scoped DB access flows.
It checks out a connection, sets the `app.tenant_id` GUC for the
transaction, and every RLS policy in the system reads that predicate.
This one helper is why sprint-08 reports, sprint-10 autocomplete, and
sprint-12 generation all get tenant isolation for free.

```python
@asynccontextmanager
async def tenant_connection(pool, tenant_id) -> AsyncIterator[Connection]:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)",
                               str(tenant_id))
            yield conn
```

RLS-first (the policy is the enforcement, app code is not trusted to
filter) is ratified in sprint-02's ADR-0007, but the helper lands here.

### 2.4 `PIISafeFilter` (ADR-0005) — logging can't leak PHI

A logging filter in `libs/observability` that redacts a fixed field set
before anything is written to any sink:
`patient_*`, `transcript`, `audio_*`, `name`, `email`, `phone`, `ssn`,
`address`, `date_of_birth`. Installed by the observability bootstrap, so
every service gets it by importing the lib — not by remembering to.

### 2.5 Service template surface

```
GET /healthz  -> 200 liveness (process up)
GET /readyz   -> 200 readiness (deps reachable) | 503
```

`create_app()` factory (testable + `--factory` runnable), middleware
order fixed, `register_exception_handlers(app)` mapping domain errors to
RFC-7807-style responses, OTel `FastAPIInstrumentor` wired. Config is a
pydantic-settings model; `AUTH_BYPASS_DEV=true` logs a startup WARNING
and is forbidden outside local dev.

### 2.6 Container standard (ADR-0002)

Multi-stage build; final image is **distroless + nonroot**. No shell,
no package manager in the runtime layer. Trivy scans on every build;
CRITICAL/HIGH CVEs fail the pipeline.

---

## 3. Work plan (day-by-day, as-built)

**Day 1 — Workspace + tooling (ADR-0001)**
- Root `pyproject.toml` as `uv` workspace; `.python-version` = 3.12.
- `Makefile`: `doctor`, `dev-up`, `smoke`, `ci`, `test`.
- `make doctor` checks Docker/Python/uv/make/git with remediation hints.

**Day 2 — `libs/secret` (ADR-0003)**
- `Secret[T]` with `.reveal()`, redacting repr/str, full unit tests
  (no leak via f-string, %-format, `json.dumps`, traceback).

**Day 3 — `libs/db` + RLS helper (ADR-0004)**
- `tenant_connection`; first migration scaffold under
  `infra/postgres/migrations/`; a smoke RLS test proving cross-tenant
  reads return zero rows.

**Day 4 — `libs/observability` (ADR-0005)**
- OTel bootstrap (traces/metrics/logs), `PIISafeFilter`, JSON logging
  with `trace_id` correlation. Unit test: each redacted field never
  reaches the formatter output.

**Day 5 — `services/_template`**
- FastAPI factory, `/healthz` + `/readyz`, middleware, exception
  handlers, pydantic-settings config, OTel instrumentation. Health
  endpoints covered by tests.

**Day 6 — Dev stack + container standard (ADR-0002)**
- Compose stack (Postgres/Redis/MinIO/Kafka/Keycloak/OTel/Loki/Tempo/
  Prometheus/Grafana). Distroless+nonroot Dockerfile for the template.
- `make dev-up` + `make smoke` (curls every health endpoint).

**Day 7 — CI gate**
- Workflow: lint(ruff) → typecheck(mypy --strict) → test(pytest) →
  security → import-linter → container-scan(trivy). `make ci` mirrors
  it exactly. Pre-commit hook rejects `os.environ` outside `config.py`.

**Day 8 — Docs + ADRs**
- `docs/adr/` README + template + ADR-0001…0005.
- `docs/onboarding.md` (30-min target), `docs/glossary.md` seed.
- SIGN-OFF, SPRINT-TODO, MEMORY index entry.

---

## 4. Verification protocol

No placeholders, no stubs in shipped code (`RULES.md`).

1. `make doctor` prints all `✓` on a clean machine.
2. `make dev-up && make smoke` — every service health endpoint 200.
3. `make ci` green: lint, mypy --strict, pytest, security,
   import-linter, container-scan.
4. **Secret leak test**: `Secret[str]` never reveals its value through
   repr/str/json/traceback (unit suite green).
5. **RLS test**: with `app.tenant_id = A`, a query over a seeded
   two-tenant table returns only A's rows; B's are invisible.
6. **PII log test**: each field in the redaction set is replaced before
   it can reach any sink.
7. **Template smoke**: a fresh `cp -r services/_template …` boots,
   serves `/healthz`+`/readyz`, and emits one OTel span per request
   with a matching `trace_id` in logs.
8. **Container gate**: trivy finds no CRITICAL/HIGH in the template
   image; image runs as nonroot (uid ≠ 0) with no shell.
9. **Determinism**: `uv sync` from a clean cache resolves identically
   against `uv.lock`.

---

## 5. Audit kinds (new)

None. The hash-chained audit log arrives in sprint-02 (ADR-0008).
Sprint-00 establishes only the logging/observability plane.

---

## 6. Security & privacy

- **Secrets**: never plaintext in code, logs, or images; `Secret[T]`
  everywhere sensitive; `.env.local`/`*.pem`/`*.key` gitignored.
- **Isolation**: RLS predicate primitive shipped (`tenant_connection`);
  policy enforcement ratified sprint-02.
- **PHI in logs**: structurally impossible for the redacted field set
  via `PIISafeFilter`.
- **Runtime**: distroless+nonroot shrinks attack surface; trivy gate.
- **Dev backdoor**: `AUTH_BYPASS_DEV` loudly warns and is prod-forbidden.

---

## 7. Glossary additions

uv workspace, `Secret[T]`, `.reveal()`, `tenant_connection`,
`app.tenant_id` GUC, RLS predicate, PIISafeFilter, distroless, nonroot,
service template, `make doctor`, readiness vs liveness, import-linter,
container scan, ADR.

---

## 8. Risks

| # | Risk | Mitigation |
|---|------|-----------|
| E1 | `uv` immaturity / bus factor (Astral) | Standard `pyproject.toml`; switch-back is mechanical; re-evaluate sprint-16 (ADR-0001) |
| E2 | Engineers bypass `tenant_connection` with raw pool access | import-linter rule + review; RLS policies (sprint-02) make raw access return nothing anyway |
| E3 | New redaction-worthy field added later, filter not updated | Field list reviewed each sprint that adds PHI columns; DPO sign-off |
| E4 | Template drift — services diverge from the skeleton | "copy `_template`" is the only sanctioned path; periodic template-conformance check |
| E5 | Compose stack heavy on laptops | Documented friction file; selective `make dev-up` profiles |
| E6 | mypy/ruff version drift local vs CI | `make ci` mirrors CI exactly; versions pinned in workspace |
| E7 | Distroless complicates debugging | Debug via a sidecar/ephemeral container, never by shelling the prod image |

---

## 9. Out of scope (deliberate)

- Authentication / Keycloak / JWT — sprint-02 (ADR-0006).
- RLS *policy* enforcement & tenant model — sprint-02 (ADR-0007).
- Hash-chained audit log — sprint-02 (ADR-0008).
- Any ML / ASR path — sprint-03 (ADR-0009).
- Real business services (dictation, reports, autocomplete) —
  sprints 04+.
- KMS-backed key management — sprint-16 (sprint-03 ships file-based).
- Production cloud topology / HPA — sprint-16.

---

## 10. Definition of done

Sprint-00 ships when, and only when:
(a) §4 verification fully green on a clean machine;
(b) a freshly copied service from `services/_template` boots and passes
    health + tracing + log-correlation with zero hand-written infra;
(c) ADR-0001…0005 published with the standard template;
(d) `docs/onboarding.md` demonstrably gets a new engineer to a green
    `make ci` inside 30 minutes;
(e) CI gate enforced on `main` (no merge while red);
(f) sign-offs in `docs/signoffs/sprint-00.md` checked (tech lead,
    security, SRE).

---

## 11. Demo script

1. Clean clone → `make doctor` all green.
2. `make dev-up && make smoke` — health endpoints 200.
3. `cp -r services/_template services/demo`, boot it, hit `/healthz`,
   show the span in Tempo and the correlated JSON log in Loki.
4. Attempt to log a `Secret[str]` and a `patient_name` — show both
   redacted.
5. Seed two tenants; run the same query under each `app.tenant_id` —
   show isolation.
6. Show CI failing on an injected CRITICAL CVE, then passing once
   removed.

---

## Appendix — As-built conformance (reconstruction audit)

Verified present in the repo at reconstruction time (2026-06-14):

| Spec item | As-built location | Status |
| --------- | ----------------- | ------ |
| uv workspace + single lockfile | `pyproject.toml`, `uv.lock` | ✓ |
| Python pin 3.12 | `.python-version` = `3.12.7` | ✓ |
| `Secret[T]` + leak tests | `libs/secret/` (+ tests) | ✓ |
| `tenant_connection` RLS helper | `libs/db/` (+ tests) | ✓ |
| OTel bootstrap + `PIISafeFilter` | `libs/observability/` (+ tests) | ✓ |
| Service template (factory, health, mw, handlers, config, OTel) | `services/_template/` | ✓ |
| Distroless + nonroot image | `services/_template/Dockerfile` (`gcr.io/distroless/python3-debian12:nonroot`) | ✓ |
| ADR-0001…0005 + README + template | `docs/adr/` | ✓ |
| Dev stack (pg/redis/minio/kafka/keycloak/otel/grafana/…) | `infra/compose/{base,dev,gpu}.yml` | ✓ |
| CI gate (lint/typecheck/test/security/import-linter/trivy) | `.github/workflows/ci.yml` | ✓ |
| `make doctor / dev-up / smoke / ci / test` | `Makefile` | ✓ |
| `os.environ` guard outside `config.py` | `.pre-commit-config.yaml` → `scripts/dev/check-no-os-environ.py` | ✓ |
| Onboarding (30-min target) + glossary | `docs/onboarding.md`, `docs/glossary.md` | ✓ |
| Sign-off register | `docs/signoffs/sprint-00.md` | ✓ (this reconstruction) |

The two artifacts that did not exist before this reconstruction were the
sign-off register (DoD §10f) and the project-memory entry; both were
added to close the sprint formally. No production code gaps were found —
the foundation is as-built and downstream sprints (01–10, A/B) build on
it directly.
