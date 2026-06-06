# Contributing

## Development environment

See [docs/onboarding.md](docs/onboarding.md). Short version:

```bash
make doctor
make dev-up
make ci   # mirrors the CI gates
```

## Branching

- `main` is always shippable.
- Branch off `main` for any change. Short-lived branches; rebase rather
  than merge into your PR branch.
- Branch names: `<type>/<short-slug>` — `feat/encounters-search`,
  `fix/auth-refresh-leak`, `chore/uv-lock-bump`.

## Commits

[Conventional Commits](https://www.conventionalcommits.org/) — enforced by
`commitizen` in pre-commit. Common types: `feat`, `fix`, `chore`, `docs`,
`refactor`, `test`, `build`, `ci`. Imperative subject under 70 characters.

```
feat(observability): add bootstrap() single entry point

…why this matters / what changed at a slightly higher level…
```

## Pull requests

- Open against `main`. Link the ticket / sprint task.
- Fill out the PR template, including the **security / PII checklist**.
- CI must be green for `lint`, `typecheck`, `test`, `security`,
  `import-linter`, and `container-scan` before review.
- Require one approval. Security-adjacent changes (auth, encryption,
  audit, secrets, RLS) require an additional review from the security
  lead.

## ADRs

Add an ADR for any irreversible decision: a runtime choice, a security
primitive, an API contract other code depends on. The PR template has an
"ADR needed?" checkbox; if unsure, ask in the PR rather than skipping.

## Release process

Sprint 01 ships local-dev only. Production release process is captured in
Sprint 16.

## Reporting security issues

See [SECURITY.md](SECURITY.md). Do **not** open a public GitHub issue for
a vulnerability.
