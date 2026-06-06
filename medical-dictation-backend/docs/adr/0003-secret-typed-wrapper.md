# ADR-0003 — Typed `Secret[T]` Wrapper

**Date:** 2026-05-09
**Status:** Accepted
**Deciders:** Backend tech lead, Security lead

---

## Context

Sensitive values — passwords, refresh tokens, OTP secrets, recovery codes,
API keys, KEK / DEK material — leak through the *normal* uses of a Python
string: `repr()` in a stack trace, `str()` in a log line, `f"..."` in an
error message, `json.dumps(...)` of a settings model, `pickle.dumps` for a
queue payload. Every leak is an irreversible disclosure. Asking engineers
to "remember" not to do these things is doomed; we want a type-system
tripwire that fails closed.

## Decision

Introduce **`Secret[T]`** at `libs/secret`. Any sensitive value flows
through the wrapper. Reading the underlying value is explicit
(`s.value()`); every other channel (`__repr__`, `__str__`, `__format__`,
default JSON, `pickle`, `copy`, `deepcopy`) returns the constant
`Secret(***)` mask or raises.

Pydantic v2 integration registers a custom serializer that emits
`"<redacted>"`, so dumping settings models doesn't disclose values.

## Consequences

**Positive**

- A leak now requires the developer to write `s.value()` explicitly. That
  call is greppable and reviewable.
- The mask is the same constant everywhere, so log diffs / search
  pipelines can audit for `Secret(***)` occurrences.
- Pydantic settings dumps are safe to log.

**Negative**

- Equality on arbitrary `T` falls back to `==`, which may not be
  constant-time. We do constant-time compare for `str` / `bytes` payloads.
- Memory is not encrypted. The plaintext lives in the heap until GC.
  Sprint 16 introduces a KMS-backed `Secret[T]` that decrypts on demand
  and never materialises plaintext outside a context manager.
- Wrapping non-secrets in `Secret` makes debugging painful. The README
  draws the line: only secret material goes in.

## Alternatives considered

- **Plain `str` discipline** — what we have today across the industry. It
  fails by leaking; the cost of a single incident dwarfs the cost of the
  wrapper.
- **`pydantic.SecretStr`** — works for strings only, no generic. We want
  `Secret[bytes]` for raw key material and `Secret[Mapping]` for some
  webhook payloads.
- **Vault-only approach** — secrets never leave the secret store. Future
  work; today most secrets are config-injected, and we still want
  defence-in-depth on values already in process memory.

## Trigger conditions for revisiting

- KMS-backed `Secret[T]` lands in Sprint 16.
- A leak via a channel we didn't anticipate (e.g. logging library
  introspection) is found.
