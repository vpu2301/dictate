# Runbook — break-glass access to a report

**Audience:** tenant administrators, and whoever reviews what they did.
**Decision record:** ADR-0033. **Permission matrix:** `docs/auth/permissions.csv`.

An administrator holds no standing access to clinical records. Break-glass
is how they read **one** report when they genuinely need to, and how
everyone else finds out that they did.

## When to use it

Legitimate: a patient complaint you must answer, a legal or regulatory
demand, a billing dispute, a quality review, continuity of care, a
correction to a record whose author has left.

Not legitimate, and visible as such in the log: curiosity, checking on a
colleague, reading your own family's records, or "it was faster than
asking the clinician". Every grant carries your name, the reason you
picked, and what you typed.

If you find yourself breaking glass routinely on the same patients, the
answer is a role change (add `clinician` to the account, or delegate the
task), not a habit.

## Doing it — UI

1. Open **Patients** → the patient → the **Reports** tab. This list is
   metadata only (title, author, date); an admin can see it without a
   grant, which is how you find the right report.
2. **Request access** on the row.
3. Pick a **reason**. Choosing *Other* requires a written justification of
   at least 10 characters.
4. Re-enter **your own password**. This proves you are at the keyboard;
   a live session is not enough on its own.
5. The report opens. The grant lasts **60 minutes** by default
   (`MDX_PHI_ACCESS_GRANT_TTL_MINUTES`) and covers **that report only**.

Opening a report link directly works too: the 403 turns into the same
request dialog, pre-targeted at that report.

## Doing it — API

```bash
# 1. Step up. Single-use, 5 min, bound to you and to this purpose.
TICKET=$(curl -sS -X POST "$AUTH/auth/reauth" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"password":"…","purpose":"phi_access_request"}' | jq -r .reauth_ticket)

# 2. Spend it.
curl -sS -X POST "$REPORT/v1/phi-access-requests" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"resource_id\":\"$REPORT_ID\",\"reason_code\":\"legal_request\",
       \"reason_note\":\"Court order 12/2026\",\"reauth_ticket\":\"$TICKET\"}"

# 3. The ordinary read now succeeds — and is counted.
curl -sS "$REPORT/v1/reports/$REPORT_ID?purpose=audit" -H "Authorization: Bearer $TOKEN"
```

`GET /v1/phi-access-requests/reasons` returns the reason vocabulary, the
grant TTL and the note minimum. Do not hard-code the codes — they are
pinned by a DB CHECK.

## What happens as a result

| Where | What lands there |
|---|---|
| Audit chain | `phi_access.granted` (`sec`) with your reason **and note**; `phi_access.used` (`sec`) per read; `report.viewed_full` / `report.pdf_rendered` escalated to `sec` with `break_glass: true` |
| The report's authors | An in-app `phi_access.granted` notification naming you, the report code and the reason (never the note) |
| `phi_access_requests` | The durable row: window, use count, last use |
| Metrics | `mdx_phi_access_granted_total{reason_code}`, `mdx_phi_access_rejected_total{cause}` |

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
| `403 role_denied` opening a report | Not an admin (e.g. an auditor) | Auditors read the grant log, never the reports. Nothing to request. |
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
