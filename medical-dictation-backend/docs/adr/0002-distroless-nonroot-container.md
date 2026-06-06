# ADR-0002 — Distroless, Nonroot Production Containers

**Date:** 2026-05-09
**Status:** Accepted
**Deciders:** Backend tech lead, SRE/DevOps, Security lead

---

## Context

Service container images are the deployment unit. Every byte of a base image
is in our supply chain: every shell, package manager, or default user is a
foothold. CVEs in `apt`, `bash`, or `coreutils` follow us into production
even when our own code is clean. Reducing surface area is the cheapest
hardening lever available.

## Decision

* Build production images with a **multi-stage** Dockerfile.
* Final stage is **`gcr.io/distroless/python3-debian12:nonroot`**: no shell,
  no package manager, no setuid binaries. The image runs as the `nonroot`
  user (UID 65532) by default; we never override.
* Build with `--provenance=true --sbom=true`. Sign images with **cosign**
  (key in CI secrets). SBOM published as a CI artifact in CycloneDX format.
* CI gates fail the build on Trivy HIGH or CRITICAL CVEs unless explicitly
  allow-listed with a justification and an expiry date.

## Consequences

**Positive**

- The runtime attack surface is dramatically reduced.
- A compromised process has no shell to escalate from and no `apt-get` to
  pull in tooling.
- SBOM + signature give us a clean audit trail for compliance.

**Negative**

- Debugging a distroless image without `sh` requires a sidecar / ephemeral
  debug container. Documented in the runbook stub; production debug pattern
  formalised in Sprint 16.
- Some libraries that shell out at runtime fail. We trade ergonomics for
  hardness; if a needed tool genuinely requires a shell, we ship the dev
  variant (`--target=debug`) for that workflow only.

## Alternatives considered

- **Alpine + s6-overlay** — small, but `musl` causes wheel-compatibility
  issues with PyTorch / asyncpg in some matrix combinations.
- **Debian slim** — fine baseline, but ships a shell and apt; no security
  win over distroless.
- **Wolfi** (Chainguard) — comparable hardness; reconsider in Sprint 16
  alongside the production registry decision.

## Trigger conditions for revisiting

- Google deprecates the distroless base.
- A repeated debugging-pain incident shows the ergonomics cost outweighs
  the security gain.
