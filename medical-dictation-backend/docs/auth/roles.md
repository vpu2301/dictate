# Roles

The realm defines five roles. The permission matrix lives at
`docs/auth/permissions.csv`; this file is its prose companion.

| Role           | Holds                                                   | Cannot                       |
| -------------- | ------------------------------------------------------- | ---------------------------- |
| `tenant_admin` | Onboarding, deactivation, MFA reset, audit read/verify, tenant settings | Cross-tenant operations      |
| `clinician`    | Routine clinical user. Tenant-read. Reports (sprint 4+) | User admin; audit            |
| `nurse`        | Limited clinical user. Same as clinician minus report sign | Most admin                |
| `auditor`      | Read-only audit + tenant context                        | Any write                   |
| `service`      | Machine-to-machine token identity                       | Any user-facing operation today |

There is **no global / cross-tenant super-admin role**. The DBA superuser
exists in the database but is never used by service code (ADR-0007).

## Picking a role at invite time

- A clinician who also runs the practice → assign **both** `tenant_admin`
  *and* `clinician`. A user can hold multiple realm roles.
- Compliance officer / external auditor → `auditor`. Doesn't need
  `tenant_admin`; the audit endpoints are independently role-gated.
- Read-only stakeholder who just needs login → `clinician` for now;
  finer-grained read-only role is a sprint-17 add.
- A machine that calls our API on a partner's behalf → `service`. The
  scope mechanism (Day 7) is wired for service tokens but sprint 02
  doesn't yet enforce per-scope checks.

## Changing a user's role

For sprint 02 there is no `/admin/users/{sub}/role` endpoint — role
changes go through the Keycloak admin console or via the Keycloak Admin
API. A future sprint adds the endpoint + an `audit.role_changed` event
(severity `sec`).

## How role changes propagate

- Existing access tokens keep their old roles until they expire (up to
  15 minutes). For immediate revocation, follow the runbook's
  "Suspected token theft" path: `POST /admin/users/{sub}/deactivate`
  also calls `/logout` which revokes refresh + active sessions in
  Keycloak.
- Next refresh after a Keycloak-side role change carries the new role
  set in the new access token (Keycloak rebuilds the claim set on
  refresh, not on access-token verify).
