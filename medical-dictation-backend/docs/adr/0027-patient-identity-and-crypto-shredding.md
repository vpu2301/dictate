# ADR-0027 — Patient identity (ІПН) & the crypto-shredding strategy

- **Status:** accepted
- **Date:** 2026-07-15
- **Sprint:** 11 (patients — lawful identity, DSAR, right to erasure)
- **Related:** ADR-0007 (RLS-first tenancy), ADR-0011 (three-layer envelope),
  ADR-0022–0024 (KEP signing), migration `0042_patient_identity_ipn.sql`

Three linked decisions that the erasure workflow (step 04), the fan-out
map (step 05), and the erasure engine (step 07) build on. They are one
ADR because they share a single premise: **a patient is a join point,
not a copy point** — identity lives in exactly one row, and everything
else references it.

## Decision A — crypto-shredding = enumerate-and-delete (no per-patient KEK)

**Chosen:** the erasure engine *enumerates* every blob and row belonging
to the patient via the fan-out map (step 05) and destroys each one:
object-store blobs are deleted outright, and the per-object wrapped DEKs
that also live in row metadata (`audio_files.envelope_metadata`,
`patients.ipn_dek`) are nulled. A per-object DEK dies with its object;
nothing else can decrypt what it protected.

**Rejected:** an additional per-patient KEK layer
(`KEK_tenant → KEK_patient → DEK_object`), where erasure would destroy
one patient key.

**Why:** a patient-KEK layer only *guarantees* destruction if every
patient blob is provably under that KEK — which is exactly the
enumeration problem again, now with a key hierarchy on top. The fan-out
map makes enumeration complete and **CI-guarded** (a new patient-data
table that isn't registered in the map fails the build), so the KEK
layer adds key-management surface (creation, wrapping, caching,
rotation interplay with ADR-0011) for no additional guarantee. If a
future requirement makes single-key destruction necessary (e.g. offline
backups outside the enumeration domain), revisit then.

## Decision B — ІПН storage: HMAC always, raw only behind a DPO-gated flag

The 10-digit ІПН (РНОКПП) is how Ukrainian clinical practice actually
identifies a patient. It is stored as:

- **`patients.ipn_hmac`** — HMAC-SHA256 of the normalized, checksum-valid
  ІПН under `MDX_PATIENT_IPN_HMAC_KEY`. Written whenever an ІПН is
  provided. This is the *only* value queries touch (exact roster lookup,
  duplicate prevention via a partial unique index that excludes
  `erased` rows). The raw ІПН appears in no SQL predicate, no response,
  no log (the observability deny-list masks `ipn`), no audit payload.
- **`patients.ipn_encrypted` + `patients.ipn_dek`** — envelope-encrypted
  raw ІПН (ADR-0011 layers; AAD = `tenant_id ‖ patient_id`; packed as
  fixed-layout `iv‖tag‖ct` / `dek_iv‖dek_tag‖wrapped_dek` columns).
  Populated **only** when `PATIENT_IPN_RAW_ENABLED=true`, which defaults
  to false and stays false pending DPO sign-off (tracked in `todo.md`).
  Nulling `ipn_dek` crypto-shreds the raw value — the step-07 engine
  uses exactly that.

**Key separation:** `MDX_PATIENT_IPN_HMAC_KEY` (patient space,
core-service) is deliberately independent from `SIGNER_IPN_HMAC_KEY`
(signer space, signing-service, S09), though both use the shared
implementation in `libs/crypto/ipn.py`. A join across the two spaces
("did the patient sign?") is not a requirement; if the DPO ever wants
signer↔patient matching, unifying the keys is a config change plus a
re-HMAC of one space — not a schema change.

**Rotation:** rotating the HMAC key orphans every stored `ipn_hmac`.
Rotation therefore requires a re-HMAC migration under maintenance
(decrypt-or-recapture, re-HMAC, swap key). No rotation machinery is
built now — this is a documented operational procedure, not code.

**Erasure branch (consumed by step 07):** at erasure, `ipn_hmac` **is
nulled** together with the raw columns — identity destruction is total,
and the partial unique index already excludes `erased` rows so a
returning patient can re-register either way. The trade-off: after
erasure the system cannot warn "this ІПН was erased before" (no
tombstone match). That is the GDPR-correct default — remembering the
hash of an erased identity is itself retained personal data. DPO
confirmation tracked in `todo.md`.

## Decision C — no user-GUC RLS in core-service

core-service keeps the as-built RLS shape: tenant-predicate policies
(`tenant_id = current_setting('app.tenant_id')`) plus RESTRICTIVE, with
role/permission checks in the service layer (`requires()` →
`libs/auth.check`). The sprint-10 retro helper (per-request
`app.user_id` / `app.user_role` GUCs for row-ownership policies) is
**not** wired here.

**Why:** no core-service table has a row-ownership policy — every
policy is tenant-scoped, and ownership semantics (e.g. "only the author
edits a draft note") are enforced in domain code where they can return
proper 403s. Adding user GUCs now would be dead configuration.

**Revisit trigger:** the moment any core-service table needs a
row-ownership *RLS* policy (not just a service-layer check), wire the
GUC helper first and write the policy against it — do not emulate
ownership with service-only checks for data whose isolation matters.
