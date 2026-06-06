# ADR-0024 — Canonical JSON for signing via RFC 8785 (JCS)

- Status: accepted
- Date: 2026-05-14
- Sprint: 09
- Deciders: tech lead, security lead, legal counsel

## Context

The bytes that get signed must be deterministic across:

- Python runtimes (3.12, future 3.13).
- Service replicas (different machines, different glibc).
- Storage round-trips (JSONB → asyncpg.Record → Pydantic → dict).
- Re-canonicalisation by an independent verifier (legal counsel may
  commission an external auditor).

Three options:

1. **Plain `json.dumps(obj, sort_keys=True)`**. Works *almost*. Edge
   cases: number formatting (`1.0` vs `1`), Unicode escapes, NaN/Inf
   handling, key collision behaviour all differ across implementations.
2. **JCS (RFC 8785)**. A spec, with a vetted implementation
   (`rfc8785` PyPI). Already used by sprint-02's audit hash chain.
3. **Roll our own**. Strictly more risk; no benefit.

## Decision

Use **RFC 8785 (JSON Canonicalization Scheme)** via the `rfc8785`
library. Centralise in `audit.canonical.canonicalize`; sprint-09's
`medical_kep.canonicalize_report` calls into it. One implementation
for both audit chain and signing.

The canonical input shape is captured in
`medical_kep.canonicalize.CanonicalReportInput`. The shape is the legal
contract for signing — any change requires a new ADR + a bump of
`CANONICAL_VERSION` + a forward-only migration (previously-signed
envelopes keep their original version forever).

## Consequences

Positive:
- Byte-stable across implementations.
- The audit chain and signing share the same primitive, so a verifier
  who validates the audit log gets signing canonicalisation "for free".
- The `rfc8785` library is small, vetted, and pure-Python — easy to
  embed in any future external verifier we ship.

Negative / accepted:
- Adding a field to the canonical shape is not free — we have to bump
  `CANONICAL_VERSION` and accept that signed envelopes from prior
  versions are signed against the old shape (and must remain
  verifiable for 25 years).
- We can't sneak in non-JSON-natural types. UUIDs → strings, datetimes
  → ISO-8601 strings, bytes → base64 strings; the canonicaliser raises
  on anything else.

## Links

- `audit.canonical` (RFC 8785 implementation).
- `medical_kep.canonicalize` (sprint-09 wrapper).
- `medical_kep.canonicalize.CanonicalReportInput`.
- `libs/kep/tests/unit/test_canonicalize.py`.
- ADR-0022 (PAdES + embedded JSON).
- Sprint-09 spec §2.3, §2.4.
