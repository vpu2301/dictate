# Sprint 01 — Service Template Hardening & Developer Paved Road
## Canonical Specification (CTO-Level) — *reconstructed as-built*

**Type:** Engineering specification — architecture, work plan, verification protocol, risk register
**Owner:** Backend tech lead
**Reviewers:** every backend engineer, security lead, SRE/DevOps lead
**Status:** Accepted (reconstructed from the as-built repo; canonical text was originally in conversation history)
**Reconstructed:** 2026-06-14
**Depends on:** sprint-00 (workspace, libs/secret, libs/db, libs/observability, `services/_template`, dev stack, CI gate)
**Hands off to:** sprint-02 (Keycloak auth + RLS policies + audit), and every service sprint thereafter

> **Reconstruction note.** Rebuilt from `.pre-commit-config.yaml`,
> `CONTRIBUTING.md`, `docs/onboarding.md`, the `Makefile` (`ci` /
> `ci-with-db` targets), `services/_template/pyproject.toml` (mypy
> strict, bandit, coverage), and the `scripts/dev/check-*` gates. Where
> the original day-by-day differed, the as-built repo is authoritative.
> Two deltas between the original plan and the as-built repo are recorded
> in §12 (As-built deltas) rather than silently smoothed over.

---

## 0. Why this sprint exists

Sprint-00 produced the foundations: the workspace, three shared libs, a
service template, a dev stack, and a CI gate. But foundations rot
without **enforcement**. Sprint-01 turns the conventions of sprint-00
into mechanically-enforced rules so a tired engineer at 5pm physically
cannot merge an unsafe pattern.

The deliverable is again not a feature — it is a **paved road with
guardrails**: pre-commit hooks, custom lint gates, an import-boundary
policy, contributor process, and a template hardened to the point where
copying it is the only sane way to make a service. Every later sprint's
uniformity (sprint-03 worker, sprint-08 reports, sprint-12 generation)
is paid for here.

The principle from sprint-00 sharpens: *the unsafe path must not
compile.* Not "discouraged in review" — blocked by a hook.

---

## 1. Scope

### 1.1 In scope

1. **Pre-commit suite** (`.pre-commit-config.yaml`): ruff (+format),
   end-of-file/trailing-whitespace/merge-conflict/private-key checks,
   yaml/toml/large-file guards, **gitleaks**, a fast **mypy** subset on
   the security-critical libs, **commitizen** (Conventional Commits on
   `commit-msg`), and two **local custom gates** (§2.2).
2. **Custom enforcement gates** (`scripts/dev/`):
   - `check-no-os-environ.py` — env reads only in `config.py`.
   - `check-no-direct-asyncpg.py` — DB access only via
     `libs/db.tenant_connection` (ADR-0004).
3. **Import-boundary policy** (import-linter): services may depend on
   `libs/*`, never on each other; `libs/*` never import `services/*`.
4. **Hardened service template**: strict mypy config, bandit security
   lint, coverage config, `.env.example`, `config.py` pydantic-settings
   pattern as the *only* env surface, fixed middleware order, exception
   handlers, `/healthz` + `/readyz` semantics nailed down.
5. **`make` command surface finalised**: `doctor`, `dev-up`, `smoke`,
   `lint`, `typecheck`, `test`, `security`, `ci` (mirrors CI exactly),
   `pre-commit-install`.
6. **Contributor process**: `CONTRIBUTING.md` (branching, Conventional
   Commits, PR template + **security/PII checklist**, ADR-needed
   checkbox), `SECURITY.md` (private vuln reporting), PR template.
7. **Onboarding contract**: clean machine → green `make ci` in ≤ 30 min,
   with a friction-capture loop (`docs/onboarding-friction.md`).

### 1.2 NOT in scope — see §9.

---

## 2. Architecture & contracts

### 2.1 The enforcement layering

Three rings, fastest-feedback first:

```
 pre-commit (local, < 2s)  ─►  make ci (local, mirrors CI)  ─►  CI on PR (authoritative)
   ruff/gitleaks/                lint typecheck test            same gates, clean runner,
   custom gates/commitizen       security + custom gates        + container-scan + import-linter
```

The contract: **`make ci` is byte-for-byte what CI runs.** If it's
green locally it's green on the runner; drift is a bug to fix, not
tolerate. (`docs/onboarding.md` codifies "don't push if `make ci` is
red".)

### 2.2 Custom gates (the two that encode sprint-00's ADRs)

**`no-os-environ-in-services`** — rejects any `os.environ` read outside
a `config.py`. Forces every configuration value through a
pydantic-settings model, which is where `Secret[T]` typing and
validation live. Excludes `config.py`, tests, `libs/secret`, `scripts/`.

**`no-direct-asyncpg`** — rejects `asyncpg.connect` anywhere except
`libs/db/`. Forces all DB access through `tenant_connection`, so no code
path can accidentally talk to Postgres without the `app.tenant_id` GUC
set. This is what makes RLS (sprint-02) un-bypassable in practice.

Both are real scripts with exit codes, run in pre-commit **and** in
`make ci`, so they can't be skipped with `--no-verify`.

### 2.3 Import-boundary policy (import-linter)

```
services/<a>  ──►  libs/*            ✅ allowed
services/<a>  ──►  services/<b>      ❌ forbidden (services are isolated)
libs/<x>      ──►  services/*        ❌ forbidden (libs never know services)
libs/<x>      ──►  libs/<y>          ✅ allowed (shared composition)
```

Cross-service communication is over HTTP/queues, never imports. This
keeps services independently deployable and is why sprint-16 can scale
them separately. The contracts live in `[tool.importlinter]` in
`pyproject.toml`; the leaf-lib contracts (`secret`, `observability`,
`*_models`) and per-service layering contracts (routers → domain →
adapters) are checked by the same tool. Run via `make lint-imports`.

### 2.4 Hardened template surface (the copy-me contract)

`services/_template/` after this sprint guarantees, by construction:

- `config.py` — the *only* place env vars are read; sensitive fields
  typed `Secret[T]`; `.env.local` for dev, `.env.example` committed.
- `main.py` — `create_app()` factory; middleware order
  (RequestID → exception handlers → OTel) fixed; `--factory` runnable.
- `mypy strict = true`, `python_version = 3.12`; **bandit** security
  lint; **coverage** source configured.
- `/healthz` (liveness, process up) vs `/readyz` (readiness, deps
  reachable → 200/503) semantics tested.
- `AUTH_BYPASS_DEV=true` logs a startup WARNING; prod-forbidden.

A new service is `cp -r services/_template services/<name>`, rename the
package, and start writing domain code — never infra.

### 2.5 Contributor contract

- **Branching**: `main` always shippable; `<type>/<slug>` branches;
  rebase not merge.
- **Commits**: Conventional Commits, commitizen-enforced on
  `commit-msg`.
- **PRs**: against `main`; PR template with **security/PII checklist**
  + "ADR needed?" checkbox; one approval; security-adjacent changes
  (auth, encryption, audit, secrets, RLS) require an extra security-lead
  review.
- **Green-or-no-merge**: lint, typecheck, test, security, import-linter,
  container-scan.

---

## 3. Work plan (day-by-day, as-built)

**Day 1 — Pre-commit baseline**
- `.pre-commit-config.yaml`: ruff(+fix)+ruff-format, the
  pre-commit-hooks set (eof, trailing-ws, merge-conflict,
  detect-private-key, check-yaml/toml, large-files ≤1024kb).
- `make pre-commit-install` (installs commit + commit-msg hooks).

**Day 2 — Secret scanning + commit hygiene**
- gitleaks hook (history-safe secret scan).
- commitizen on `commit-msg`; document the format in `CONTRIBUTING.md`.

**Day 3 — Custom gate: env discipline**
- `scripts/dev/check-no-os-environ.py` + wire into pre-commit and
  `make ci`. Tests: a stray `os.environ` in a service fails; one in
  `config.py` passes.

**Day 4 — Custom gate: DB discipline**
- `scripts/dev/check-no-direct-asyncpg.py` + wire in. Tests: a direct
  `asyncpg.connect` in a service fails; usage via `tenant_connection`
  passes; `libs/db` itself is exempt.

**Day 5 — Import-boundary policy**
- import-linter contracts (service↔service forbidden; libs↛services).
- Wire `make lint-imports`. Deliberately introduce a violation in a
  scratch branch to prove it fails, then remove.

**Day 6 — Template hardening**
- mypy `strict`, bandit, coverage in `services/_template/pyproject.toml`.
- `.env.example`; `config.py` as sole env surface; `/healthz`+`/readyz`
  tests; middleware order test.

**Day 7 — `make ci` consolidation + onboarding**
- `ci` target = lint typecheck test security + custom gates, mirroring
  CI; `ci-with-db` variant for DB-dependent gates.
- `docs/onboarding.md` 30-min target + `docs/onboarding-friction.md`
  loop; `CONTRIBUTING.md`, `SECURITY.md`, PR template.

**Day 8 — Docs + sign-off**
- Glossary additions; ensure ADR-0001…0005 cross-links resolve.
- SIGN-OFF, RETRO, SPRINT-TODO, MEMORY index entry.

---

## 4. Verification protocol

No placeholders, no stubs in shipped code (`RULES.md`).

1. `pre-commit run -a` green on a clean tree.
2. **Env gate**: committing `os.environ["X"]` in a service file is
   rejected; the same read inside `config.py` passes.
3. **DB gate**: committing `asyncpg.connect(...)` in a service is
   rejected; `tenant_connection` usage passes; `libs/db` exempt.
4. **gitleaks**: a planted fake AWS key is caught pre-commit.
5. **commitizen**: a non-conventional commit message is rejected on
   `commit-msg`.
6. **import-linter**: a service→service import fails `make lint-imports`;
   removing it restores green.
7. **Template**: `cp -r services/_template services/_probe` → boots,
   `/healthz` 200, `/readyz` 200 when deps up / 503 when down; mypy
   `--strict` clean; bandit clean.
8. **`make ci` == CI**: the local target runs the same gate set; a
   change green locally is green on the runner.
9. **Onboarding**: a fresh engineer (or a clean container) reaches green
   `make ci` within 30 minutes, friction logged.

---

## 5. Audit kinds (new)

None. Hash-chained audit arrives in sprint-02 (ADR-0008). Sprint-01 is
process + enforcement, no runtime audit surface.

---

## 6. Security & privacy

- **Secrets can't be committed**: gitleaks + detect-private-key +
  gitignored `.env.local`/`*.pem`/`*.key`.
- **Secrets can't be mis-read**: env discipline gate funnels everything
  through `config.py` + `Secret[T]`.
- **DB can't be reached unsafely**: asyncpg gate funnels through
  `tenant_connection` (pre-conditions RLS in sprint-02).
- **Supply chain**: pinned hook revs; container-scan in CI; large-file
  guard.
- **Process**: security/PII PR checklist; mandatory security-lead review
  on security-adjacent diffs; `SECURITY.md` private disclosure path.

---

## 7. Glossary additions

pre-commit, gitleaks, commitizen, Conventional Commits, custom gate,
`no-os-environ`, `no-direct-asyncpg`, import-linter, import contract,
bandit, coverage source, `make ci` mirror, paved road, green-or-no-merge,
security/PII checklist, liveness vs readiness.

---

## 8. Risks

| # | Risk | Mitigation |
|---|------|-----------|
| E1 | Engineers bypass hooks with `git commit --no-verify` | The same gates run in `make ci` **and** CI; CI is authoritative and can't be skipped |
| E2 | Local mypy/ruff version drift vs CI | Revs pinned in `.pre-commit-config.yaml`; `make ci` mirrors CI; documented in onboarding friction table |
| E3 | Custom AST gates produce false positives | Conservative matching + explicit excludes (`config.py`, tests, `libs/secret`, `scripts/`); unit-tested both ways |
| E4 | Import-linter slows CI on a large graph | Contracts scoped to top-level packages; runs in seconds; revisit if PR latency grows |
| E5 | Template drift — services diverge after copy | "copy `_template`" is the only sanctioned path; periodic conformance check; sprint-retros catch drift |
| E6 | gitleaks noisy on historical test fixtures | Allowlist documented fixtures; never blanket-disable |
| E7 | Onboarding rots silently | `onboarding-friction.md` loop; each new hire updates it; 30-min target is a tested DoD item |

---

## 9. Out of scope (deliberate)

- Authentication / Keycloak / JWT verification — sprint-02 (ADR-0006).
- RLS *policies* + tenant model + audit log — sprint-02
  (ADR-0007/0008). Sprint-01 ships the *primitives' enforcement*, not
  the policies.
- Any ML/ASR path — sprint-03.
- Production release process, cloud topology, HPA, KMS — sprint-16.
  (`CONTRIBUTING.md`: "Sprint 01 ships local-dev only.")
- Real business services — sprints 04+.

---

## 10. Definition of done

Sprint-01 ships when, and only when:
(a) §4 verification fully green on a clean machine;
(b) every custom gate provably rejects its target anti-pattern and
    permits the sanctioned one (both directions tested);
(c) `make ci` runs the identical gate set CI runs (no drift);
(d) `cp -r services/_template …` yields a service that passes
    mypy `--strict`, bandit, and health checks with zero infra edits;
(e) `CONTRIBUTING.md` + `SECURITY.md` + PR template (with security/PII
    checklist) merged and enforced on `main`;
(f) onboarding 30-min target demonstrated;
(g) sign-offs in `docs/signoffs/sprint-01.md` checked (tech lead,
    security, SRE).

---

## 11. Demo script

1. Try to commit `os.environ["DB"]` in a service file → hook rejects;
   move it to `config.py` → passes.
2. Try to commit `asyncpg.connect(...)` in a service → rejected; switch
   to `tenant_connection` → passes.
3. Plant a fake secret → gitleaks blocks the commit.
4. Write `fix stuff` as a commit message → commitizen rejects; write a
   Conventional Commit → accepted.
5. Add a `services/a → services/b` import → `make lint-imports` fails on
   import-linter; remove → green.
6. `cp -r services/_template services/demo` → boot, `/healthz` 200,
   mypy `--strict` clean, bandit clean.
7. Show `make ci` output matching the CI gate list.

---

## 12. As-built deltas (reconstruction honesty)

Two points where the as-built repo diverges from the original plan text.
Recorded here so a future reader trusts the doc rather than discovering
the gap themselves.

1. **`lint-imports` is a standalone target, not folded into `make ci`.**
   The as-built `ci:` target is `lint typecheck test security
   check-audit-insert check-no-object-storage check-no-crypto
   validate-templates`. The import-boundary contract is enforced via
   `make lint-imports` (and in CI), but a developer running `make ci`
   locally does not get it for free. Either fold it into `ci:` or keep
   the split deliberate — flagged for the sprint-01 sign-off to decide.
2. **`docs/onboarding-friction.md` was not created.** `docs/onboarding.md`
   exists and carries the 30-minute target, but the friction-capture
   loop (§1.7, §3 Day 7, risk E7) has no backing file yet. The loop is
   therefore aspirational, not enforced. Create the file or drop the
   claim before close.

Everything else in §1–§11 matches the repo as built: the pre-commit
suite (ruff/format, the hooks set, gitleaks `v8.18.4`, the mypy subset on
`libs/(secret|observability)`, commitizen on `commit-msg`, both local
custom gates with their excludes), the import-linter contract set, the
hardened template, and the contributor docs.
