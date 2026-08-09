# Runbook — break-glass access to a report or a patient record

**Audience:** anyone who opens a record that is not theirs, and whoever
reviews what they did.
**Decision record:** ADR-0033 (incl. the S15 amendment and the
signing-authority / break-glass hotfix).
**Permission matrix:** `docs/auth/permissions.csv`.

An administrator holds no standing access to clinical records — and,
since S15, no standing access to a patient's demographics, timeline,
visit history or anamnesis either. The roster you see is redacted to
name + id.

**Since the hotfix, break-glass is no longer an admin-only door.** Holding
`report.read` or `patient.read_full` now answers only *"may you open
charts at all"*. Whether you may open **this** chart is a second
question, and the answer is your **treatment relationship** with the
patient.

## Who has a treatment relationship

You do, if any of these is true (`libs/clinical_access` — one definition,
used identically by report-service and core-service):

- you are the **primary author** of a report for that patient;
- you are a **co-author** of one;
- you are the clinician who **opened one of their encounters**.

You do not, otherwise — and that includes every `tenant_admin`, and every
clinician who has simply never treated this person. The predicate never
looks at roles. Being an administrator cannot create a relationship, and
being a clinician does not make every patient in the clinic yours.

Your own patients open exactly as they always did: no reason, no
step-up, no challenge. Nothing about the ordinary clinical day changes.

## When to use it

Legitimate, administrative: a patient complaint you must answer, a legal
or regulatory demand, a billing dispute, a quality review, continuity of
care, a correction to a record whose author has left.

Legitimate, clinical (the hotfix reason codes): **emergency care** — a
patient who is not yours needs treating now; **care coordination** — a
covering shift, a corridor consult, a handover; **patient request** —
they asked you to look; **technical support** — you are debugging a
record rather than reading it (this one requires a written note, because
"support" covers everything from a rendering bug to reading a whole
chart).

Not legitimate, and visible as such in the log: curiosity, checking on a
colleague, reading your own family's records, or "it was faster than
asking the clinician". Every grant carries your name, the reason you
picked, and what you typed.

If you find yourself breaking glass routinely on the same patients, the
system is telling you something true: either the relationship is real and
is not being recorded — **open an encounter**, which is the workflow act
that creates it — or the account needs a role change. A habit of
breaking glass is a finding, not a workaround.

## Doing it — UI

It is a two-step door since S15: glass on the **patient** first, then —
only if you need a document's content — glass on the **report**. Two
grants, two reasons, two audit trails.

1. Open **Patients** and find the patient by name — the redacted roster
   needs no grant.
2. Open the patient. The 403 turns into the **Request access** dialog,
   pre-targeted at that patient.
3. Pick a **reason**. Choosing *Other* requires a written justification of
   at least 10 characters.
4. Re-enter **your own password**, and — if your account is enrolled in
   MFA — a current **TOTP code**. This proves you are at the keyboard;
   a live session is not enough on its own. Accounts without MFA
   enrolment step up on the password alone (MFA is off by default in the
   pilot), and the grant records which of the two you actually proved.
5. The record opens — demographics, timeline, visit history — for
   **60 minutes** by default (`MDX_PHI_ACCESS_GRANT_TTL_MINUTES`),
   covering **that patient only**.
6. To read a report's content from the timeline, **Request access** on
   the row — the separate, per-report grant as before.

Opening a report link directly works too: the 403 turns into the same
request dialog, pre-targeted at that report.

## Doing it — API

```bash
# 1. Step up. Single-use, 5 min, bound to you and to this purpose.
TICKET=$(curl -sS -X POST "$AUTH/auth/reauth" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"password":"…","purpose":"phi_access_request"}' | jq -r .reauth_ticket)
# Enrolled in MFA? The call above returns 401 {"code":"totp_required"};
# add "totp_code":"123456". The 200 response's `factors` field says what
# the ticket proved: ["password"] or ["password","totp"].

# 2. Spend it. resource_kind is 'report' (default) or 'patient' (S15).
curl -sS -X POST "$REPORT/v1/phi-access-requests" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"resource_kind\":\"patient\",\"resource_id\":\"$PATIENT_ID\",
       \"reason_code\":\"legal_request\",
       \"reason_note\":\"Court order 12/2026\",\"reauth_ticket\":\"$TICKET\"}"

# 3. The ordinary read now succeeds — and is counted.
curl -sS "$CORE/patients/$PATIENT_ID" -H "Authorization: Bearer $TOKEN"
# (report-kind grants unlock the report read instead:)
# curl -sS "$REPORT/v1/reports/$REPORT_ID?purpose=audit" -H "Authorization: Bearer $TOKEN"
```

`GET /v1/phi-access-requests/reasons` returns the reason vocabulary, the
grant TTL and the note minimum. Do not hard-code the codes — they are
pinned by a DB CHECK.

## What happens as a result

| Where | What lands there |
|---|---|
| Audit chain | `phi_access.granted` (`sec`) with your reason **and note**; `phi_access.used` (`sec`) per read; `report.viewed_full` / `report.pdf_rendered` — or `patient.viewed` / `patient.updated` for patient-kind grants — escalated to `sec` with `break_glass: true` |
| The report's authors | An in-app `phi_access.granted` notification naming you, the report code and the reason (never the note). Patient-kind grants notify nobody — a patient record has no author; the trail and the oversight list are the control |
| `phi_access_requests` | The durable row: window, use count, last use |
| Metrics | `mdx_break_glass_total{reason,resource_kind,role,principal,tenant_id}` (the alerting signal), plus the S14 `mdx_phi_access_granted_total{reason_code}` and `mdx_phi_access_rejected_total{cause}` |
| Alerts | `BreakGlassBurstByPrincipal` (page — >5 grants in 15 min by one person: the chart-sweep signature), `BreakGlassSustainedByPrincipal` (warn — >20/24h), `BreakGlassTenantRateElevated` (warn — >25/h tenant-wide). Rules: `infra/prometheus/rules/hotfix-break-glass.yml` |

## Reviewing (auditor or admin)

```bash
# Everything, most recent first.
curl -sS "$REPORT/v1/phi-access-requests?limit=100" -H "Authorization: Bearer $TOKEN"
# Only what is open right now — the "who can read what at this moment" question.
curl -sS "$REPORT/v1/phi-access-requests?active_only=true" -H "Authorization: Bearer $TOKEN"
```

Read `use_count` alongside the reason. A grant requested and never used
is a different fact from one used eleven times, and only the second is a
pattern.

To close a grant early — including after it has expired, when the point
is to record that the reason did not hold up:

```bash
curl -sS -X POST "$REPORT/v1/phi-access-requests/$GRANT_ID/revoke" \
  -H "Authorization: Bearer $TOKEN"
```

## Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `401 reauth_required` on step 2 | Ticket expired (5 min), already spent, or minted for a different user/purpose | Redo step 1. The dialog does this without losing your typed reason. |
| `401` on step 1 | Wrong password | Retype. Repeated failures trip Keycloak's brute-force detector and lock the account (`auth.reauth_failed`, `sec`). |
| `403 role_denied` opening a report | Your role holds neither a standing clinical read nor break-glass (e.g. an auditor) | Auditors read the grant log, never the reports. Nothing to request. |
| `403 phi_access_required` as a **clinician**, on a patient you expected to be able to open | HOTFIX: you have no treatment relationship with them | If they genuinely are your patient, the relationship is missing rather than absent — **open an encounter**, and the ordinary path returns. If they are not, break glass with `emergency_care` / `care_coordination`. |
| `401 totp_required` on step 1 | Your account is MFA-enrolled | Re-send with `totp_code`. |
| `403 phi_access_required` after granting | Grant expired, was revoked, or is for a **different** report | Check `active_only=true`; request again for the report you actually need. |
| `422 reason_note_required` | `other` with a note under 10 chars | Say what the access is for. |
| `404` on step 2 | No such report in this tenant | Wrong id. RLS makes another tenant's report indistinguishable from a nonexistent one — this is deliberate. |

## Operational notes

- Spent and expired reauth tickets are swept opportunistically on each
  `/auth/reauth` call (older than 1 day). They hold no PHI and no residual
  authority; there is no cron.
- `phi_access_requests` is never deleted — the DELETE policy is
  `USING (false)`. Expiry is a timestamp, not a row removal.
- Rolling back migration `0056` **destroys the reason notes**. The audit
  chain keeps the `phi_access.granted` events, but export the table first
  on any environment that has served real traffic.
- Rolling back migration `0074` narrows the reason vocabulary back to the
  S14 seven. Grants written with a clinical code are **not deleted** —
  the down migration rewrites them to `other` and prepends the original
  code to the note, so the history survives in readable form.
- If `BreakGlassTenantRateElevated` fires across a whole tenant rather
  than for one principal, suspect the relationship predicate before
  suspecting the staff: a regression in `libs/clinical_access`, or
  encounters not being written, makes every clinician look like a
  stranger to their own patients. Check that before opening an
  investigation into anyone.
