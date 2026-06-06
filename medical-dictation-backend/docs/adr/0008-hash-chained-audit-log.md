# ADR-0008 — Hash-Chained Audit Log + `audit_writer` Escape Hatch

**Date:** 2026-05-11
**Status:** Accepted
**Deciders:** Backend tech lead, Security lead, DPO
**Builds on:** ADR-0004 (tenant_connection), ADR-0007 (RLS-first)

---

## Context

Ukrainian medical-record retention law and Article 30 of GDPR require
tamper-evident audit logs of access and mutation to personal-health
data. "Tamper-evident" means: an actor with administrative database
access who silently edits a row leaves a detectable trace. A plain
append-only table is not tamper-evident — a superuser can `UPDATE` or
`DELETE` rows and the log shows no anomaly afterward.

The standard cryptographic primitive for tamper-evidence is a hash
chain (the same idea as a blockchain, minus the consensus): each row
commits to a hash of the previous row's contents, so any single-row
edit invalidates every subsequent hash and a verifier detects the seq
of divergence.

We need a writer that the application code calls easily, a verifier
that a nightly job can run unattended, and a database schema that's
hostile to in-place editing even when the attacker has DBA access.

## Decision

A single Postgres table `audit.events` with columns:

- `tenant_id UUID`, `seq BIGINT` — composite primary key.
- `created_at`, `actor_sub`, `actor_role`, `kind`, `target_kind`,
  `target_id` — denormalised at write.
- `payload_jcs JSONB` — the full event record, JSON-canonical per RFC
  8785, queryable.
- `prev_hash BYTEA`, `payload_hash BYTEA` — the chain links.
  `payload_hash = sha256(prev_hash || jcs_bytes(payload_jcs))`. Genesis
  row has `prev_hash` set to 32 zero bytes.
- `severity` — `info | warn | sec | error`.

The chain is **per-tenant** — every tenant has its own monotonic seq.
This keeps verification parallel, makes per-tenant retention policies
trivial, and avoids a global counter as a contention point.

A trigger raises on any `UPDATE` or `DELETE` against `audit.events`,
regardless of role. The only path that bypasses the trigger is
`TRUNCATE` (DBA-only and itself logged at the Postgres-log level).

### The `audit_writer` role

A dedicated Postgres role with `INSERT, SELECT, UPDATE` on
`audit.events`. The `UPDATE` is *only* to allow `SELECT … FOR UPDATE` to
acquire a row lock on the last seq — the trigger blocks every actual
UPDATE statement. `app_role` and `tenant_writer` have **zero**
permissions on `audit.events`.

The library `libs/audit.AuditWriter` is the only sanctioned writer:

1. Acquires a connection from an `audit_writer`-credentialed pool.
2. Wraps in a `READ COMMITTED` transaction.
3. Takes a per-tenant advisory lock (`pg_advisory_xact_lock`) — this is
   the *primary* concurrency primitive. Without it, two concurrent
   first-writes for a tenant both compute seq=1.
4. `SELECT … FOR UPDATE` on the last seq row to additionally protect
   against contention once the table is non-empty.
5. Computes the event_record dict, canonicalises with RFC 8785,
   computes `sha256(prev_hash || jcs_bytes)`.
6. Inserts. Retries on `SerializationError` / `UniqueViolationError`
   (defensive — never observed in tests).

The role is the *one* documented escape from the
`tenant_connection`-only contract of ADR-0004. We chose this design
deliberately: audit events have to land regardless of the caller's
tenant role; centralising the privilege in one dedicated role makes the
exception reviewable and bounded.

### The verifier

`libs/audit.AuditVerifier.verify_chain` walks the chain ordered by seq.
At each row it asserts (a) no gap in seq, (b) stored `prev_hash` matches
the running hash, (c) recomputed `sha256(running || jcs(payload_jcs))`
matches the stored `payload_hash`. On the first divergence it returns a
`VerificationReport(ok=False, first_divergence_seq=…, divergence_reason=…)`
with both expected and actual hashes for forensics.

The nightly job (`scripts/jobs/nightly_verify.py`) runs the verifier for
every active tenant, emits Prometheus gauges, and self-audits with an
`audit.chain_verified` event — itself on the chain that was just
verified.

## Consequences

**Positive**

- An attacker with DBA access who silently edits a row breaks the
  chain. The nightly verifier surfaces the seq of the edit; Postgres
  logs surface who connected.
- The writer is one library call. Misuse (forgetting to audit) is a
  matter of forgetting to call the library, not of writing
  custom-but-wrong code.
- Each tenant's chain is independent. Retention/archival can be done
  per-tenant.
- JCS canonicalisation makes hashes stable across JSON serialisation
  quirks — writer and verifier agree even if the JSONB column got
  re-serialised by Postgres.

**Negative**

- Per-tenant advisory locks serialise writes for one tenant. At the
  pilot's expected load (sub-1 event/s/tenant in steady state, bursts
  of ~50/s during high-traffic operations) this is comfortable. Beyond
  ~500 events/s/tenant the lock becomes the bottleneck.
- `UPDATE` privilege grant on `audit_writer` is conceptually broader
  than needed. The immutability trigger is the actual enforcer and is
  tested; the grant is the smaller of two bad options (the cleaner one
  would require Postgres `SELECT FOR UPDATE` without UPDATE privilege,
  which it doesn't support under RLS).
- Storing `payload_jcs` as JSONB means Postgres may re-serialise the
  canonical bytes on read. The verifier canonicalises again before
  hashing — correct, but adds a small CPU cost per verified row.

## Alternatives considered

- **External signed log (HSM)** — every event signed by a hardware
  key. Strongest tamper-evidence. Rejected for the pilot on cost and
  ops complexity; revisit at sprint 17 if pen-test or customer
  contracts require it.
- **Plain append-only table with revoked UPDATE/DELETE** — fails the
  threat model when the attacker has superuser. The chain catches that
  case; plain revoke does not.
- **Linear blockchain with PoW** — overkill; the threat is not a
  rewriting adversary with compute resources, it's a DBA-level
  insider. A hash chain suffices.
- **Streaming append to an immutable bucket (S3 Object Lock)** —
  considered for sprint 16 archival. For the live query path the
  Postgres table gives us SQL filtering + transactional semantics that
  S3 doesn't.

## Trigger conditions for revisiting

- A tenant's write rate exceeds the advisory-lock single-tenant
  throughput (~500/s) sustained. Solution paths: shard the chain by
  another key, batch writes via a write-behind queue (then audit the
  queue handoff), or move to HSM-signed events.
- Pen-test surfaces a vector where the chain detects tampering too
  late for legal compliance (e.g. by then logs are archived).
  Mitigation: stream copies to an immutable bucket in addition.
- A new "system-level event" emerges that genuinely has no tenant
  scope (e.g. cluster-wide upgrade audit). Today we'd add a synthetic
  "platform" tenant_id; if that proliferates, the schema needs a
  redesign.
