# Onboarding

Goal: from a clean machine to a running stack and a green test run inside
30 minutes. If it takes longer, capture the friction in
`docs/onboarding-friction.md` so the next person hits a smoother path.

## 0. Prerequisites

| Tool | Minimum | Why |
| ---- | ------- | --- |
| Docker (with the Compose plugin) | 25.0 / 2.20 | Local stack |
| Python | 3.12 | Pinned in `.python-version` |
| `uv` | 0.4 | Workspace dependency manager (ADR-0001) |
| `make` | 4.0 | Command surface |
| `git` | 2.40 | … |

`make doctor` checks the lot and prints remediation hints.

## 1. Clone & verify

```bash
git clone https://github.com/your-org/medical-dictation-backend
cd medical-dictation-backend
make doctor
```

Re-run `make doctor` until every line is `✓`.

## 2. Bring up the stack

```bash
make dev-up      # Postgres, Redis, MinIO, Kafka, Keycloak, OTel + Loki + Tempo + Prometheus + Grafana
make smoke       # curls every health endpoint
```

Service URLs are listed in the README.

## 3. Run the template service

```bash
cd services/_template
uv pip install -e ".[dev]"
cp .env.example .env.local            # defaults match the dev stack
uv run uvicorn template_service.main:app --reload
# In another terminal:
curl http://localhost:8000/healthz
curl http://localhost:8000/readyz
```

In Tempo (`http://localhost:16686` for now; Tempo migration in Sprint 16),
filter by `service.name=template-service` and you should see one span per
request. In Loki (via Grafana, `http://localhost:3000`) the same request
appears as a JSON log line whose `trace_id` matches the span.

## 4. First PR walkthrough

1. Create a branch off `main`.
2. Make a tiny change (e.g. a docstring update in `services/_template`).
3. `make ci` locally — it mirrors CI exactly. Don't push if it's red.
4. Open a PR; the template walks you through the security checklist.
5. CI must be green for `lint`, `typecheck`, `test`, `security`,
   `import-linter`, and `container-scan` before merge.

## 5. Common friction

| Symptom | Likely cause | Fix |
| ------- | ------------ | --- |
| `make dev-up` hangs on Keycloak | Keycloak waits on Postgres readiness ~30 s on first boot | Be patient; subsequent boots are fast. |
| `make doctor` says "port 5432 in use" | A local Postgres is running | `brew services stop postgresql` (macOS) or `sudo systemctl stop postgresql` (Linux). |
| `uv` not found | Not installed | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| `pre-commit` rejects your commit on `os.environ` | You read an envvar outside `config.py` | Move the read into the service's `config.py` Pydantic settings model. |
| Tests pass locally but `mypy --strict` fails in CI | Local mypy version drift | `uv run mypy --strict services/_template/src libs/...` |

## 6. Next steps

- Read [ADR-0003 (`Secret[T]`)](adr/0003-secret-typed-wrapper.md) and
  [ADR-0004 (`tenant_connection`)](adr/0004-rls-tenant-connection.md).
  Almost every Sprint 02 change touches one or both.
- Skim the [glossary](glossary.md).
- Open `docs/onboarding-friction.md` and add anything that bit you.
