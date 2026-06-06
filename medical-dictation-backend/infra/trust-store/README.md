# Trust Store

This directory holds the PEM bundles consumed by ``signing-service`` at
boot for KEP envelope verification.

## Files

- `ca-bundle.pem` — concatenated PEM blocks for trusted root CAs
  (КНЕДП Ukraine). Source: weekly TSL refresh from czo.gov.ua.
- `tsa-bundle.pem` — trusted TSA roots for sprint-09 LTV.
- `czo-cert.pem` — public cert used to verify `TSL.xml.p7s`.
- `test-ca-bundle.pem` — committed only in dev branches. NEVER in
  production. The mock provider's leaf cert chains up to this bundle.

## Lifecycle

1. `scripts/update-trust-store.sh` runs weekly via
   `.github/workflows/trust-store-refresh.yml`.
2. If the TSL diff is non-empty the script posts to `#security`
   Slack with the diff.
3. Security lead reviews and opens a PR replacing `ca-bundle.pem`.
4. PR merge rebuilds the signing-service container; staged rollout
   per `docs/runbooks/signing.md`.

**Never auto-apply.** Every change to this directory is a deliberate
sign-off from security lead.

## Test CA (dev only)

`libs/kep/tests/fixtures/test-ca/` is generated on first boot of the
mock provider. The mock provider's constructor raises
`RuntimeError` if `ENVIRONMENT=production` so even a stray test CA
in the trust store cannot accept mock-signed envelopes in prod.
