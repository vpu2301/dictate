# `libs/secret` — `Secret[T]`

A typed wrapper for sensitive values (passwords, tokens, recovery codes, OTP
secrets, API keys) that refuses to leak via the channels that bite teams in
practice: `repr`, `str`, `f"{}"`, `format`, default JSON serialisation,
pickling, and `copy`/`deepcopy`.

```python
from secret import Secret

token = Secret("hunter2")
print(token)              # Secret(***)
print(repr(token))        # Secret(***)
print(f"got: {token}")    # got: Secret(***)
import json
json.dumps({"t": token}, default=str)  # contains "Secret(***)", not "hunter2"

token.value()             # "hunter2"  — explicit, single read path
```

## Pydantic v2 integration

```python
from pydantic import BaseModel
from secret import Secret

class Config(BaseModel):
    api_key: Secret[str]

cfg = Config(api_key="hunter2")
cfg.api_key.value()    # "hunter2"
cfg.model_dump()       # {"api_key": "<redacted>"}
cfg.model_dump_json()  # '{"api_key":"<redacted>"}'
```

## When to use

Wrap any string or bytes that, if logged, would be a security incident:

- Passwords, password hashes pre-salting
- Refresh tokens, access tokens, API keys
- OTP / MFA secrets, recovery codes
- KEK / DEK material, private keys
- Database / Redis / S3 credentials

## When **not** to use

- For values you legitimately need to log (request IDs, tenant IDs, public
  identifiers). Wrapping non-secrets in `Secret` makes debugging painful.
- For long-lived in-memory state where you need fast hashing — `hash()` is
  type-identity, not value, and is only useful for placement in sets where
  identity is enough.

## Limitations

- A `Secret[T]` is not memory-safe: the underlying value lives in the Python
  heap and is visible to a process-memory dump or a debugger. Sprint 16 will
  introduce a KMS-backed `Secret[T]` for production secrets that never decrypt
  to in-process strings; this implementation is the local-dev / source-code
  protection layer and the migration target for that work.
- Equality is constant-time only for `str` and `bytes` payloads; for arbitrary
  types it falls back to `==`, which may not be constant-time.

## See also

- ADR-0003 — Secret[T] typed wrapper
- `scripts/dev/check-no-os-environ.py` (pre-commit) — keeps `os.environ`
  reads inside `config.py` so all secret material flows through `Secret`.
