# Sprint 21 — deployment notes (as built)

Reconciles the deployment plan against the implementation. Platform rule
unchanged: only infra in docker-compose; services via uvicorn locally.

## Name/location mapping (plan → as built)

| Plan said | As built | Why |
| --- | --- | --- |
| corpus-forge under `libs/` or `tools/` | **`services/corpus-forge/`** | uv workspace members are `services/*` + `libs/*`; it's not a lib (it has adapters + a CLI), and the non-HTTP-service precedent is asr-worker. Recorded in EXPLORE §8.2. |
| `CORPUS_JURY_URL` | **`MDX_CORPUS_LLM_BASE_URL`** (+ `_BACKEND`, `_MODEL`) | repo-wide `MDX_` convention. Default is `http://localhost:8089` (the sprint-15 llama.cpp port) — private by definition, never external. |
| `CORPUS_GENERATOR_URL` + key | **`MDX_CORPUS_EXTERNAL_API_KEY` / `MDX_CORPUS_EXTERNAL_MODEL`** | Deliberately a *different pair of variables* from the jury backend, per the plan's "must never be the same URL by accident". The external client is hardwired to api.anthropic.com; there is no URL variable to mis-point. |
| `make corpus-forge ARGS=…` | ✅ same | |
| migrations 0035–0038 | **0081–0085** | slots were long taken (EXPLORE §1). 0085 adds the review SECURITY DEFINER fn + the plan's backfill (`tier=1`, `corpus_release='v0'` on seed rows). |
| mining "with the app role" | **dedicated operator DSN** (`MDX_CORPUS_DSN`) | k-anonymity requires cross-tenant reads; app_role + RLS cannot see across tenants — that's the entire point of the gate. Runbook documents the prod read-only role. |

## The PHI boundary, deployment half — implemented

- **Refuse-to-start assertion**: every construction of the in-perimeter
  client goes through `corpus_forge.cli._local_client`, which calls
  `domain/perimeter.assert_in_perimeter_url()` — a public host (or non-http
  scheme, or dotted public hostname) raises `PHIBoundaryViolation` before
  any client exists. Static judgement, no DNS resolution (a resolver flake
  must not soften a security gate). Unit-tested for both directions.
- **Egress**: the corpus-forge host needs exactly: the in-perimeter LLM
  backend, `api.anthropic.com` (optional, public-data candidates only), and
  the dataset hosts in `infra/seeds/corpus/sources.urls`. Add those to the
  environment egress allowlist at deploy time; nothing else.
- **Log hygiene**: `make check-corpus-log-hygiene` (in `make ci`) fails any
  logger call in the corpus pipeline that references phrase text, prompts,
  or API keys. Audit payloads carry ids/counts only (event-kinds doc).

## Release loading — not migrations

`corpus-forge release --version vX` publishes from the DB (artifact +
`corpus_releases` row). Two deployment verbs, both idempotent:

- `release --apply` — the seed job: loads a committed artifact into an
  environment, verifying the CSV SHA against the manifest and the manifest
  SHA against the release register (a mismatch on an existing version
  raises — releases are immutable).
- `release --retire` — rollback: retires the release's rows (they stop
  serving via the `review_state='accepted'` trie predicate); the register
  row stays. **`--apply` never un-retires** — an incident-retired harmful
  phrase must not resurrect via a routine re-apply. Restoring a
  mistakenly-retired release is a deliberate manual UPDATE (runbook).

After either verb: bump the trie cache version (nightly rollup does it
anyway; immediately via `autocomplete:tenant_phrase_version:*`).

## Source snapshots

`make fetch-corpus-sources` → downloads into `infra/seeds/corpus/sources/`
(**gitignored**), records name/url/SHA-256/date in
`infra/seeds/corpus/sources.lock` (**committed**). Dataset URLs are an
operator fill-in (`sources.urls` — data.gov.ua resource URLs change per
revision, so no fake defaults are committed).

## CI (all in `make ci` unless noted)

- corpus-forge: ruff, **mypy --strict (CI-gated, no debt exemption)**,
  pytest (109 unit), bandit/semgrep, import-linter layering.
- `check-corpus-releases` — validator v2 over every committed release
  artifact (quota advisory for v0.x, enforced from v1.0.0).
- `check-corpus-log-hygiene` — above.
- PHI-boundary must-raise test: default unit suite, no env flag.
- `check-rls` covers the three new tables (`ci-with-db`).
- Eval-corpus manifest integrity: existing `check-corpus` target, unchanged;
  scales to 120 utterances (SHA-per-utterance, count-independent).
- Permissions drift: `corpus.review` in both `perms.py` and
  `permissions.csv` (69 auth tests green).
- Frontend 50-decision timing budget (≤400 ms/decision): frontend-repo CI —
  tracked there, not here.

## /corpus HTTP surface (added for the FE review UI)

autocomplete-service (:8007), all behind `corpus.review`:
`GET /corpus/candidates`, `POST /corpus/candidates/{id}/review`
(mode=review|audit — audit records spot-check decisions against
jury-accepted rows without changing state), `GET /corpus/stats` (incl.
quota-cell heatmap + `audit_rejections`), `GET /corpus/releases`; plus
`GET /autocomplete/phrases` provenance columns and
`POST /autocomplete/phrases/{id}/retire` (tenant/user scope only — system
rows are operator-managed). OpenAPI snapshot refreshed.

## Rollout order (plan §rollout, unchanged)

1. Migrations 0081–0085 + the trie predicate → dev; verify existing corpus
   serves (done in dev: 18 autocomplete integration tests green).
2. corpus-forge + review UI behind `corpus.review` → internal tenant only.
3. Release v1.0.0 → dev; run `scripts/eval/corpus_coverage.py`; compare to
   the pre-release baseline row in `docs/eval/sprint-21-coverage.md`.
4. Staging → 24 h soak with the "clinic day" k6 scenario; autocomplete p99
   regression guard: unchanged at ≤80 ms.
5. Production, off-peak, one tenant first. Watch
   `mdx_autocomplete_trie_size_bytes_histogram`: 10k system phrases ≈ 20×
   today's corpus and the trie memory bill is a genuine unknown — ADR-0025's
   cold-start mitigations cover the rebuild storm, not the steady-state RAM.

## Git LFS

The plan said "already wired in sprint-07 — verify". **Verification result:
it never was** — no `filter=lfs` rule existed anywhere. Now wired:
`eval/**/*.wav filter=lfs …` in `.gitattributes`, `git lfs install` added to
the README prerequisites. The 8 existing placeholder wavs stay as ordinary
blobs (tiny synthetic tones; rewriting history for them isn't worth it);
every wav added from now on — including the ~120-utterance fill and the
phone-mic subset — is an LFS pointer.
