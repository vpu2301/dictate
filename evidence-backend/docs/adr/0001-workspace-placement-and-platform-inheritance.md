# ADR-0001 — Sibling workspace with path-level platform inheritance

- **Date:** 2026-08-04
- **Status:** Accepted
- **Deciders:** Volodymyr Pugachov (product), development agent

## Context

The Evidence AI framework rules carry two placement variants: rule I2 ("evidence
services are new members of the `medical-dictation-backend` workspace") and rule
A1 ("two repositories; `evidence-backend` is a uv workspace"). The product owner
decided: a **separate workspace at `dictate/evidence-backend/`** that must start
under Docker separately from the platform stack, while inheriting the platform's
identity, users, permissions, audit chain, and patient data (rule I1/I3/I4).

## Decision

1. `evidence-backend/` is its own uv workspace (Python 3.12, own `Makefile`,
   own `make ci`), sibling to `medical-dictation-backend/` inside the same git
   repository.
2. Platform coupling points stay **in the platform tree**, not copied:
   - migrations join the platform chain (`infra/postgres/migrations/`, 0067+),
     one Postgres, one RLS regime, one audit chain;
   - permissions extend the shared matrix (`libs/auth/perms.py` +
     `docs/auth/permissions.csv`, bidirectional drift test);
   - identities live in the shared Keycloak realm export + seed.
3. Future evidence services consume platform libs (`auth`, `audit`, `db`,
   `crypto`, `storage`, `secret`, `observability`) as **path dependencies** on
   `../medical-dictation-backend/libs/*` — one source of truth, no forks.
   `libs/evidence_models` itself is a leaf and imports none of them (gate:
   `make check-leaf-imports`).
4. Evidence services get their own compose file attaching to the platform's
   Docker network (arrives with the first service, EVA-S02+).

## Consequences

- One `git clone` carries both stacks; evidence CI is a separate, fast gate.
- Cross-workspace edits (permissions, migrations) land in the platform tree and
  run the platform's own gates — evidence sprints must run both `make ci`s.
- The platform migration chain becomes shared state between two workspaces;
  migration numbers are claimed in platform order.

## Alternatives considered

- **Join the platform workspace (I2 literal):** cheapest imports, but couples
  service lifecycles and defeats the "starts separately in Docker" requirement.
- **Fully separate repo + copied libs:** clean isolation, but the copied
  crypto/audit/db libs drift from the platform's — rejected as a safety risk.

## Trigger conditions for revisiting

- Evidence stack must deploy against a *remote* platform (no shared disk) —
  path deps break; switch to published internal packages.
- The platform team objects to shared migration numbering; move to a dedicated
  schema + own runner.
