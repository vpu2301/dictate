# Sprint-09 Legal Sign-off (Template)

This template is what legal counsel completes after the day-9 review.

## Items reviewed

- [ ] Canonical document format (`medical_kep.canonicalize.CanonicalReportInput`
  shape, ADR-0024). All fields legally required for Ukrainian medical
  record retention are present.
- [ ] PAdES with embedded JSON (ADR-0022). The dual representation
  (rendered PDF + embedded canonical JSON) satisfies the
  human-readable requirement of Law 2155-VIII.
- [ ] PAdES-LTV profile sufficient for 25-year retention.
- [ ] Signer identification: full name + HMAC-of-IPN approach
  acceptable.
- [ ] Timestamp authority sourced from КНЕДП TSA meets the qualified
  timestamp requirement.
- [ ] Public verify response shape does not leak more than is required
  by inspector / court access.

## Legal counsel sign-off

| name | role | date | signature |
| --- | --- | --- | --- |
|     | external counsel |     |     |

## Conditions / caveats raised during review

(Populated by counsel; future sprints address as needed.)

## Status

- [ ] Approved as-is
- [ ] Approved with conditions
- [ ] Returned for revision
