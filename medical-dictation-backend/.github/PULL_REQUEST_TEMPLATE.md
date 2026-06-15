## Summary

<!-- One paragraph: what changed and why. Link the relevant Linear/Jira ticket. -->

## Type of change

- [ ] Bug fix
- [ ] New feature / story implementation
- [ ] Refactor (no behaviour change)
- [ ] Dependency update
- [ ] Infrastructure / CI change
- [ ] Documentation only

## Testing

<!-- Describe how you tested this. What cases did you cover? -->

- [ ] Unit tests added/updated and passing locally (`make test`)
- [ ] Coverage did not drop below 70% (`make test-cov`)
- [ ] Manual test against local stack (`make dev-up && make smoke-test`)

## Security & PII checklist

- [ ] No secrets, credentials, or tokens are committed (not even in tests)
- [ ] No PII fields (`patient_*`, `transcript`, `audio_*`, `name`, `email`) appear in log messages
- [ ] Any new API input is validated by Pydantic before use
- [ ] SQL queries use parameterised statements (no string interpolation)
- [ ] New dependencies reviewed for known CVEs (`trivy` / `pip-audit`)
- [ ] Auth is enforced on all non-health endpoints (or documented exception exists)

## Migrations

- [ ] No database migrations required
- [ ] Migration included and tested against a clean schema

## Architecture / ADR

<!-- Any change to a load-bearing decision (layering, RLS, crypto envelope,
storage boundary, audit chain, public API) needs an ADR in docs/adr/. -->

- [ ] No architectural decision changed — no ADR needed
- [ ] An ADR is needed and is included / linked in this PR
- [ ] This PR amends an existing ADR (link it) and updates the affected import-linter / CI contract

## Reviewer notes

<!-- Anything you'd like reviewers to pay special attention to. -->
