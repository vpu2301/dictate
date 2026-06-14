# Sprint 00 — Sign-off Register

**Status:** reconstructed as-built / awaiting countersign
**Sprint window:** foundation (ground floor — predates the dated sprint windows)
**Reconstructed:** 2026-06-14
**Canonical spec:** [`docs/sprints/sprint-00.md`](../sprints/sprint-00.md)

Sprint 00 is the paved-road foundation: the `uv` workspace, the three
load-bearing libs (`secret`, `db`, `observability`), the service
template, the container standard, the dev stack, and the CI gate. This
register is reconstructed against the as-built repo — each row names the
artefact and the verification that backs it. Mark `✅` only after the
linked verification has been run against the current code; reviewer name
+ date go in the columns.

---

## Tech lead (final review)

| Item                                                                                  | Reviewer | Date | Status |
| ------------------------------------------------------------------------------------- | -------- | ---- | ------ |
| `uv` workspace resolves deterministically (`uv sync` clean cache == `uv.lock`)        | ________ | ____ | ⬜     |
| Python pinned to 3.12 (`.python-version` = `3.12.7`)                                   | ________ | ____ | ⬜     |
| `services/_template` boots via `create_app()`; `/healthz` + `/readyz` covered by tests | ________ | ____ | ⬜     |
| "copy `_template`" is the only sanctioned path for a new service (no hand-written infra) | ________ | ____ | ⬜     |
| `os.environ` only inside `config.py` (pre-commit `no-os-environ-in-services` holds)    | ________ | ____ | ⬜     |
| ADR-0001…0005 published with the standard template; README index current              | ________ | ____ | ⬜     |
| `make ci` mirrors the CI gate exactly                                                  | ________ | ____ | ⬜     |

---

## Security lead

| Item                                                                                  | Reviewer | Date | Status |
| ------------------------------------------------------------------------------------- | -------- | ---- | ------ |
| `Secret[T]` never reveals its value via repr/str/format/JSON/pickle/copy/traceback (leak suite green) | ________ | ____ | ⬜     |
| Sensitive config fields typed as `Secret[…]` in the template settings                 | ________ | ____ | ⬜     |
| `PIISafeFilter` redacts the full field set (patient_*, transcript, audio_*, name, email, phone, ssn, address, date_of_birth) before any sink | ________ | ____ | ⬜     |
| `tenant_connection` is the only sanctioned tenant-scoped DB path (transaction-local `app.tenant_id`) | ________ | ____ | ⬜     |
| RLS smoke: under `app.tenant_id = A`, tenant B's rows are invisible                    | ________ | ____ | ⬜     |
| Container is distroless + nonroot (uid ≠ 0, no shell); trivy finds no CRITICAL/HIGH    | ________ | ____ | ⬜     |
| `AUTH_BYPASS_DEV=true` logs a startup WARNING and is forbidden outside local dev       | ________ | ____ | ⬜     |
| No plaintext secrets in code/logs/images; `.env.local`/`*.pem`/`*.key` gitignored     | ________ | ____ | ⬜     |

---

## SRE / DevOps

| Item                                                                                  | Reviewer | Date | Status |
| ------------------------------------------------------------------------------------- | -------- | ---- | ------ |
| `make doctor` prints all ✓ on a clean machine (Docker/Python/uv/make/git)             | ________ | ____ | ⬜     |
| `make dev-up && make smoke` — every health endpoint returns 200                       | ________ | ____ | ⬜     |
| Dev stack reproducible from clean: pg / redis / minio / kafka / keycloak / otel / prometheus / grafana / loki | ________ | ____ | ⬜     |
| CI gate enforced on `main` (no merge while red)                                        | ________ | ____ | ⬜     |
| Template request emits one OTel span with a `trace_id` matching the correlated JSON log | ________ | ____ | ⬜     |
| `docs/onboarding.md` gets a new engineer to a green `make ci` inside 30 minutes        | ________ | ____ | ⬜     |

---

## Composite

Sprint 00 is **done** when:

- All rows above are ✅.
- §4 of the canonical spec verifies green on a clean machine
  (`make doctor`, `make dev-up && make smoke`, `make ci`, Secret leak
  test, RLS test, PII log test, template smoke, container gate,
  `uv sync` determinism).
- A freshly copied service from `services/_template` boots and passes
  health + tracing + log-correlation with zero hand-written infra (DoD §10b).
- Retro doc (`docs/retros/sprint-00.md`) is filled in.

Any row left ⬜ blocks the formal sprint-00 close. As of reconstruction
(2026-06-14) every listed artefact exists in the repo; the rows await a
named reviewer countersign against the running code.
