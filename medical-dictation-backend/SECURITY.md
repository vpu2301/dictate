# Security Policy

## Reporting a vulnerability

Please do **not** open a public GitHub issue for a security vulnerability.

Email the security lead directly: **security@medical-dictation.example**
(replace once the alias is provisioned).

Include:

- A description of the issue and its impact.
- Reproduction steps or proof-of-concept.
- Affected commit / branch / version.
- Your contact details.

We aim to acknowledge a report within 48 hours and triage within five
business days. Critical issues that touch PHI or authentication are
prioritised over feature work.

## Supported versions

| Version | Supported |
| ------- | --------- |
| `main`  | ✅ — pre-release; the only supported branch |

Once we cut a release branch, this table is amended in a follow-up PR.

## Disclosure

We coordinate disclosure with the reporter. The default is private
disclosure until a fix has shipped to all affected deployments; we publish
a public advisory after that, crediting the reporter unless they prefer
anonymity.

## Scope

In scope:
- Authentication / authorisation flows.
- Tenant-isolation invariants (RLS / `tenant_connection`).
- Audit-chain integrity.
- PHI / PII handling — including logs and traces.
- Secret material handling (`Secret[T]`).
- Container hardening (distroless, nonroot, signing).

Out of scope (low impact, please file a normal issue):
- Documentation typos.
- Cosmetic UI bugs in dev-only dashboards.
