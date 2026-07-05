# ADR-0026 — Server-side file-key signing via UAPKI; dev_password scaffold

- Status: accepted
- Date: 2026-07-03
- Sprint: 09 (revision)
- Deciders: tech lead, security lead, legal counsel (pending review), SRE/DevOps

## Context

The S09 revision makes **file-key signing the primary MVP qualified
path**: most Ukrainian clinicians already hold a КНЕДП-issued file key
(`Key-6.dat`, `.pfx`, `.jks`), so signing with it needs no third-party
contract and no per-operation cost, unlike Дія.Підпис (business
agreement pending). That requires DSTU 4145-2002 signing +
ДСТУ 7564/GOST 34.311 hashing server-side — algorithms the `cryptography`
package does not implement.

Candidates evaluated (2026-07-03):

1. **UAPKI** (github.com/specinfo-ua/UAPKI) — open-source C/C++ PKI
   stack from the ЦЗО software vendor (СПЕЦІНФОСИСТЕМИ), holding a
   Ukrainian state cryptographic expertise conclusion
   (№ 04/05/02-2096, 21.07.2021). BSD-2-Clause. Single C ABI entry
   point `process(json)` (JSON task API). Native support for
   PKCS#12 / JKS / **ІІТ Key-6.dat** / PKCS#8 containers, loadable
   **fully in memory** (`storage: "file://memory"`). CAdES-BES/T/C/XL/A
   + TSP/OCSP/CRL. Prebuilt linux-amd64 `.so` per release; only
   runtime dep is libcurl. Actively maintained (commits 2026-06).
2. **ІІТ EUSignCP** — the proprietary de-facto standard. Technically
   proven headless on Linux, but no published license for server-side
   embedding/redistribution; practitioners report it as a commercial
   agreement with ІІТ. A procurement item, not a dependency.
3. **Pure-Python / dstucrypt / dstu-engine** — nothing maintained
   exists on PyPI; `dstu-engine` (OpenSSL ENGINE API, deprecated) has
   no CAdES-T/TSP. Not viable.

## Decision

**UAPKI, in-process via a thin ctypes wrapper**
(`medical_kep.uapki_backend.UapkiBackend`), serialised behind a module
lock and executed in a worker thread. Flow per sign request:
`INIT` (once, lazy) → `OPEN` (`file://memory` + container bytes +
password) → `KEYS`/`SELECT_KEY` → `SIGN` (detached CAdES over the
canonical JCS bytes; CAdES-T when a TSA is configured, degrading to
CAdES-BES with a logged warning) → `CLOSE`. `VERIFY` provides the
DSTU-capable cryptographic check that complements the structural
`verify_envelope` pass (document-hash binding, trust-store anchoring,
validity window).

Verified on Linux before adoption (spec mandate): UAPKI v2.0.12
linux-amd64 in a `python:3.12-slim-bookworm` container signed the DSTU
test container (`test-diia.p12`) over canonical JSON bytes and VERIFY
returned `TOTAL-VALID`; a one-byte tamper returned `TOTAL-FAILED`
(`messageDigest=INVALID`); a wrong password failed `OPEN` with
errorCode 1035 `INVALID_MAC`. Fixtures vendored under
`libs/kep/tests/fixtures/uapki/` (BSD-2 attribution in its README);
integration tests gate on `RUN_UAPKI_INTEGRATION=1`.

Packaging: the four release `.so` files (~0.9 MB gzipped, glibc-linked)
are baked into the signing-service image at `/opt/uapki` from the
pinned v2.0.12 release with checksum verification. `FileKeyProvider`
is wired only when `UAPKI_LIB_DIR` exists, so dev machines without the
libs run every other provider unaffected.

Key-material hygiene: the container travels request-scoped in memory
only; our working `bytearray` copies are zeroed after use. Python's
immutable `str`/`bytes` crossing the ctypes boundary cannot be
provably zeroed — a documented limitation accepted for the pilot
(the process is short-lived per request path, memory is never swapped
in the container profile, and the key never touches disk or logs).
Legal note: Law 2155-VIII expects the private key under the signer's
sole control — server-side use during the request requires explicit
signer consent (the sign UI carries it) and we never persist container
or password. The client-side alternative (M1·B4 local-KEP PAdES
upload) remains available for tenants that refuse server-side custody.

### dev_password scaffold (development only)

`dev_password` re-authenticates the clinician with the SAME Keycloak
password grant login uses and records a `signature_level='dev'`
envelope: canonical hash + signer identity, **no cryptographic
envelope**. Three independent production guards:

1. `DevPasswordProvider.__init__` raises on `ENVIRONMENT=production`.
2. signing-service config **rejects** `SIGNING_DEV_PASSWORD_ENABLED`
   when `ENVIRONMENT=production` (pydantic model validator); the
   registry only wires the provider when the flag is on.
3. CI gate `check-no-dev-signing-in-prod-config` fails the build if a
   production-looking config enables the flag.

`signature_level` is immutable at the DB layer (trigger, migration
0035) and cross-CHECKed against provider; a `dev` envelope can never
carry `is_qualified=true` (CHECK) and every surface (API, /verify,
PDF watermark, audit) reports the tier truthfully. Removal before
launch = flip the flag, delete `dev_password_provider.py` +
`keycloak_password.py`, drop the registry wiring.

## Consequences

- Qualified file-key signing works offline against any КНЕДП key with
  zero external contracts; Дія remains the mobile-UX path.
- We own a ~300-line ctypes shim and the UAPKI version pin; upgrades
  re-run the Linux sign/verify probe as a release gate.
- The signing worker is serialised per process — adequate for pilot
  volumes; scale-out is horizontal (more service replicas).
- ІІТ EUSignCP stays a fallback pending licensing terms.
