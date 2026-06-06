# ADR-0001 — Python Version Pin and `uv` Workspace

**Date:** 2026-05-09
**Status:** Accepted
**Deciders:** Backend tech lead, Sprint 01 implementers

---

## Context

The backend is a monorepo of FastAPI services and shared libraries. We need:

1. A Python version that supports the type-system features we depend on
   (`Generic` with `__class_getitem__`, structural `Protocol`, PEP-695 syntax)
   without forcing engineers onto a daily-build interpreter.
2. A dependency manager that resolves and installs across many workspace
   members fast enough to keep CI cycle time under 8 minutes.

## Decision

* Pin Python to **3.12**. The `.python-version` file pins a specific patch
  release used by every developer and the CI runner, so resolved wheels are
  deterministic. Bumping the patch is an ADR amendment, not a casual update.
* Use **`uv`** (Astral) with a workspace declaration in the root
  `pyproject.toml`. Each service and lib has its own `pyproject.toml`; the
  workspace lets `services/_template` import `libs/observability` without
  publishing.

## Consequences

**Positive**

- `uv sync` resolves the entire workspace in seconds; the install step in CI
  drops from minutes to under 30 seconds.
- Standard `pyproject.toml` / PEP-517 keeps the door open for switching back
  to pip / poetry / pdm with no source-code changes.
- Workspace mode means cross-cutting changes ship in a single PR.

**Negative**

- `uv` is young. Switching back is mechanical, but the bus factor on Astral
  is real. The next quarterly check-in (Sprint 16) re-evaluates.
- Engineers must install `uv` locally; `make doctor` flags missing tooling.

## Alternatives considered

- **Poetry** — slower, no native workspace, lock format is non-standard.
- **pip + pip-tools** — supported, but no workspace; the cross-package
  refactor toil would dominate Sprint 1 and 2.
- **PDM** — close to `uv`, smaller community, lock format is non-standard.

## Trigger conditions for revisiting

- Astral abandons or relicenses `uv`.
- A Python 3.12 → 3.13 migration discovers an ecosystem gap.
- A workspace bug causes a release-blocking incident.
