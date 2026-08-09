# ADR-0033 — Administrators are separated from PHI; break-glass is the door

- **Status:** Accepted (amended 2026-08-02 and 2026-08-09 — see the
  amendments below)
- **Date:** 2026-07-24
- **Supersedes in part:** ADR-0006 (the permission matrix's original
  "tenant_admin is a superset of clinician" shape)
- **Related:** ADR-0027 (patient identity), ADR-0028 (privacy ops),
  ADR-0031 (notifications carry pointers, not PHI)

## Context

Until now `tenant_admin` held every clinical permission the matrix
defines: `asr.*`, `dictation.*`, `report.read`/`report.write`, `note.*`.
The role was modelled as "clinician, plus administration", so an account
created purely to manage users and billing could browse every patient's
notes, dictations and reports in the tenant, and `is_treatment_team()`
additionally gave it a tenant-wide bypass of search-snippet redaction.

That is the wrong default for a system holding clinical records. An
administrator's job — users, roles, tenant settings, templates, the audit
trail, the patient roster — needs none of it. Under a minimum-necessary
reading it is a standing grant with no standing need.

At the same time, "administrators can never read a report" is not
survivable either. Real requests arrive that only an administrator can
answer: a patient complaint, a lawyer's demand, a billing dispute, a
correction to a record whose author has left. A control that cannot be
used when it is genuinely needed does not get respected; it gets routed
around, usually by handing someone a clinician account.

Two further forces shaped the design:

- Nurses and clinicians needed the **opposite** change on the same
  surfaces. The notes feed showed a bare UUID where the patient's name
  belonged, and the report list showed initials frozen at creation time
  (so a renamed or erased patient kept stale initials forever).
- `report.read` and `report.write` had been reused to gate
  autocomplete-service, with a note in `permissions.csv` deferring
  granular scopes "until a role needs the distinction".

## Decision

### 1. Drop the clinical permissions from `tenant_admin`

`asr.*`, `dictation.*`, `report.read`, `report.write` and `note.*` are
now clinician/nurse only. `patient.read`/`patient.write` stay: the roster
is the surface an admin's job actually needs, and it is where they find
the report to request.

This is a matrix over **roles, not people**. A practising doctor who also
administers the tenant holds both `tenant_admin` and `clinician`, and
`check()` passes on any granting role — their access is unchanged. Only
an admin-ONLY account loses the clinical surfaces. (The dev seed's
`admin@tenant-a` holds both; this is deliberate and test-pinned.)

`is_treatment_team()` loses its `tenant_admin` branch for the same
reason. `dpo` keeps it.

### 2. Break-glass: immediate, justified, time-boxed, loud

`POST /v1/phi-access-requests` mints a grant over **one report** after
two things:

- a **reason** from a closed vocabulary (`patient_complaint`,
  `legal_request`, `billing_dispute`, `quality_review`,
  `care_continuity`, `data_correction`, `other` — the last requiring a
  written note); and
- a **password re-entry**, exchanged at `POST /auth/reauth` for a
  single-use, 5-minute, purpose-bound ticket that this endpoint consumes
  atomically.

The grant lasts 60 minutes by default, is counted on every use, and is
revocable. `GET /v1/phi-access-requests` is the oversight log
(tenant_admin + auditor).

**There is no approval step.** This was the load-bearing choice. An
approval queue is a stronger control on paper, but the situations that
justify break-glass are exactly the ones where nobody is available to
approve — and an unusable control is worse than an after-the-fact one,
because it teaches people to obtain a clinician login instead. The
control is therefore displaced to **after** the act, and made impossible
to miss:

- `phi_access.granted` / `.used` / `.denied` / `.revoked` in the audit
  chain at `sec` severity, with the reason note;
- a `phi_access.granted` notification to the report's authors, so the
  clinician whose record it is learns of it without reading a log;
- `report.viewed_full` and `report.pdf_rendered` are escalated to `sec`
  and carry `break_glass: true` when served under a grant.

### 3. `stats.read` — PHI-free aggregates

Removing `report.read`/`dictation.read`/`asr.read` from `tenant_admin`
would have broken the business dashboard, which aggregates those three
list endpoints into counts and timings. Rather than either accept the
breakage or hand the permissions back, a single new action
`stats.read` on `tenant` admits an administrator to those lists in a
stripped projection:

| Endpoint | Stripped for a `stats.read`-only caller |
|---|---|
| `GET /v1/reports/search` | title, snippet, `patient_id`, both patient-name fields, ICD-10 codes |
| `GET /asr/jobs` | patient fields, `result_url`, `error_detail` |
| `GET /dictate/sessions` | nothing — `SessionSummary` is already PHI-free (the transcript lives on `SessionDetail`, behind `dictation.read`) |

Implemented via `auth.check_any()` plus a per-service `requires_any()`
dep; the handler branches on `auth.can_claims()` to choose the
projection.

### 4. Split autocomplete off `report.*`

`autocomplete.read` / `autocomplete.write` on a new `phrase` target kind.
A role now needs the distinction the CSV anticipated: curating the tenant
phrase library is administration, not clinical authorship, so an admin
keeps it while losing `report.*`.

### 5. Full patient names for clinical roles

- `GET /notes` and `GET /notes/{id}` embed `patient: {id, name:{uk,en}}`
  via a LEFT JOIN. (The SPA already expected this shape and had been
  falling back to the raw UUID.)
- `GET /v1/reports/search` adds `patient_name: {uk, en}`, resolved live
  per page rather than read from the frozen
  `reports.patient_name_redacted`. This also fixes the staleness bug: the
  initials are written once at creation, so renames and erasures never
  propagated.
- `GET /asr/jobs` carries the patient through
  `audio_files.encounter_id → encounters → patients`.

All are LEFT JOINs and all are nullable on the wire: a note or job whose
patient row is gone or RLS-invisible must still appear. Losing a name is
a display problem; losing the record is a clinical one.

## Consequences

**Good.** Minimum-necessary access is now the default rather than an
aspiration. Every administrator read of a clinical record is
individually justified, time-boxed, counted, and visible to the
clinician who wrote it. The nurse/clinician surfaces got materially
better at the same time.

**Costs, accepted.**

- An admin-only account that legitimately needs several reports must
  break glass once per report. Deliberate — a grant that spread to a
  whole patient would make the reason meaningless.
- `POST /auth/reauth` is a password-verification oracle for a caller who
  already holds a valid session. Brute-force protection is Keycloak's
  realm-level detector, which counts these grants like any other; the
  failure is audited at `sec`. Accepted rather than adding a second
  rate limiter.
- The reason note lives in `phi_access_requests` and in the audit chain,
  but deliberately NOT in notifications (ADR-0031's allow-list admits
  `reason_code` only): it is free text an admin typed about a specific
  patient situation.
- Rolling `0056` back destroys the reason notes. The down migration says
  so.

**Not done.** ~~Break-glass covers `report` only (`resource_kind` is a
CHECK with one value).~~ *(Amended below: `patient` joined in S15.)*
Notes and dictations are simply invisible to an administrator, with no
door at all — widening that is a deliberate future act requiring its own
enforcement point, not a config change.

## Amendment (S15, 2026-08-02) — the patient record joins the wall

The original decision kept `patient.read`/`patient.write` as a standing
admin grant on the reasoning that "the roster is the surface an admin's
job actually needs". In practice that standing grant covered far more
than a roster: full demographics (dob, contact details, address), the
ІПН presence flag, the clinical timeline, visit history and the
anamnesis — a per-patient view of everything except the documents
themselves, readable with no reason given and audited only at `info`.
That contradicted the minimum-necessary premise of this ADR.

### What changed

- A new action `patient.read_full` (clinician/nurse only) gates one
  patient's demographics, timeline, visit history and anamnesis.
  `tenant_admin` does not hold it.
- `patient.read` — which the admin keeps — now yields a **redacted
  roster**: name + id (+ status, for the erased-tombstone listing).
  Enough to find the record to break glass on; a door you cannot find
  the handle of is a wall.
- `resource_kind` in `phi_access_requests` widens to
  `('report','patient')` (migration 0061). A patient-kind grant is
  minted through the same `POST /v1/phi-access-requests` door (same
  reason vocabulary, same reauth ticket, same TTL) and enforced by
  core-service's twin of the report guard. Kind isolation is explicit:
  a patient grant never opens a report, nor vice versa.
- An admin's `PUT /patients/{id}` rides the same grant — editing
  presumes reading. `POST /patients` (registration), consents, the
  open-visits queue, the schedule and the privacy queue stay on the
  standing permissions: they are the admin's operational job.
- Audit mirrors the report side: `patient.viewed` / `patient.updated`
  escalate to `sec` with `break_glass: true` under a grant, plus a
  distinct `phi_access.used` event per read. Refused attempts write
  `authz.denied` at `sec`.
- No notification is sent for a patient-kind grant — a patient record
  has no author to tell. The after-the-fact control is the `sec` trail
  and the oversight list. (A DPO-directed notification is a possible
  future refinement.)

### Consequence for the admin workflow

The two-step flow becomes explicit: break glass on the *patient* to see
the record and find the report in the timeline, then break glass on the
*report* to read its content. Two grants, two reasons, two audit
trails — deliberate, for the same reason a report grant never spread to
the whole patient in the original decision.


## Amendment (hotfix, 2026-08-09) — the relationship, not the role, opens the chart

### The defect

Both the original decision and the S15 amendment framed break-glass as an
*administrator's* door, and drew the wall around the `tenant_admin` role.
That framing had a hole in it the size of the clinical staff.

`report.read` and `patient.read_full` are held by every clinician and
every nurse in the tenant. Nothing downstream asked whether the caller
had anything to do with the patient. So while an administrator was
walled off from a chart and had to give a reason, re-enter a password
and accept a `sec`-severity audit row, any clinician in the building
opened the same chart with no reason, no step-up, and an `info`-level
view event. The sprint-08 read-purpose gate asked non-authors for a
`?purpose=` query string, but a free string with no vocabulary, no
step-up and no distinct audit kind is a formality, not a control.

The premise of this ADR is minimum-necessary access. A role-shaped wall
cannot express that, because "minimum necessary" is a fact about the
relationship between a clinician and a patient, not about a job title.

### What changed

- **A treatment relationship is now required for standing clinical
  reads.** `libs/clinical_access` holds the single definition, used
  identically by report-service and core-service: primary author or
  co-author of a report for the patient, or the clinician who opened one
  of the patient's encounters. Deliberately excluded: role (an
  administrator cannot acquire one, a clinician does not get one for
  free), tenant membership (RLS already scopes that), and prior access
  (circular — a break-glass read would mint the relationship that makes
  the next one routine).
- **Standing permission and relationship are now separate questions.**
  `report.read` / `patient.read_full` answer "may you open charts at
  all"; the relationship answers "is this chart yours". Both are
  required, and failing the second lands in exactly the same break-glass
  flow an administrator uses.
- **`phi_access.request` is granted to `clinician` and `nurse`.** It had
  been admin-only on the reasoning that clinicians "already hold
  `report.read`, so a grant would be meaningless" — reasoning that dies
  with the relationship check. Withholding it would have made the
  relationship a wall in precisely the situations medicine needs a door:
  a covering shift, a corridor consult, a patient who collapses in the
  waiting room.
- **Four clinical reason codes** (`emergency_care`, `care_coordination`,
  `patient_request`, `technical_support`, migration 0074) join the S14
  administrative seven. Folding clinical break-glass into `other` would
  have made the oversight log unreadable at exactly the moment it got
  busier.
- **The step-up gains a second factor when there is one to demand.**
  `POST /auth/reauth` requires a current TOTP code from principals
  enrolled under ADR-0039, and the ticket records the factors proven
  (`{password}` or `{password,totp}`). Conditional, not mandatory:
  `MDX_REQUIRE_MFA` is off in the pilot and most accounts have no
  enrolment, so an unconditional requirement would not have strengthened
  break-glass — it would have deleted it, for the 2am legal-deadline
  case the door was built for. The enrolment is read from Keycloak, not
  from the caller's `mfa` claim, so a token minted before enrolment
  cannot downgrade its own holder's step-up.
- **An anomaly signal.** `mdx_break_glass_total{reason,resource_kind,
  role,principal,tenant_id}` plus alerts on per-principal burst,
  per-principal sustained use, and tenant-wide elevation
  (`infra/prometheus/rules/hotfix-break-glass.yml`). A door that is
  loud only in a log nobody reads is not loud.

### Consequences

The ordinary clinical day is unchanged: your own patients open exactly
as before, because authorship and encounters are what the schema already
records about who is looking after whom.

The cost is a database round trip on every single-record read that
previously short-circuited on a permission check alone. It is one
indexed query and it buys the control its own premise.

The visible change is that a clinician can now be challenged on a chart,
which is new and will surprise people. Two mitigations: the refusal
carries `phi_access_required` and the resource id, so the SPA opens the
request dialog on the record they were reaching for; and a sustained
pattern of one clinician breaking glass is treated as a **workflow**
finding first — the relationship is probably real and simply not being
recorded, which means encounters are not being opened — and a conduct
finding second. `BreakGlassTenantRateElevated` exists to make a
predicate regression look like a predicate regression rather than an
outbreak of snooping.

### Not changed

Report **search** still returns snippets across the tenant to any holder
of `report.read`, without a relationship check. This is deliberate and is
the same reasoning that keeps the admin's redacted roster: search is the
discovery surface you use to *find* the record to break glass on, and
gating it would make the door unfindable. Search results are already
audited (`search_audit_buffer`) and carry snippets rather than full
content. Narrowing what a snippet may contain is a separate, worthwhile
piece of work and is not attempted here.
