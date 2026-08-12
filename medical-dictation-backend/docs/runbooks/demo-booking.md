# Runbook — demo booking emails

**Service:** `marketing-service` (`:8012` in compose)
**Owns:** the two transactional emails a prospect gets when they ask for a demo
**Schema:** `marketing` (migration `0075_marketing_demo.sql`)

---

## What it does

```
visitor submits #/signup → "Book a demo"
        │
        ├─▶ HubSpot                     (SPA, src/api/leads.js — CRM record)
        │
        └─▶ POST /public/demo/requests  (SPA, src/api/demo.js)
                    │
                    ├─ row in marketing.demo_requests
                    └─ row in marketing.demo_mail_outbox  kind=request_received
                                │
                    delivery worker drains it
                                │
                       ✉  "We have your request"  ──▶ button: Google appointment page
                                                              │
                                                    visitor picks a slot
                                                              │
                                        Google mails the invitation to sales@klarnote.com
                                                              │
                                        booking watcher polls that mailbox over IMAP
                                                              │
                                        parses the text/calendar part → demo_bookings
                                                              │
                                        outbox row  kind=demo_confirmed
                                                              │
                                               ✉  "Your demo is booked"
                                                  (slot, Meet link, .ics attached)
```

There is no webhook from Google. The invitation email **is** the event feed, which
is why one credential — the sales mailbox — covers both directions.

## Language

The email template is chosen server-side, in this order (`domain/language.py`):

1. `lang` in the request body — the UI language the visitor read the form in.
2. The request's country — `CF-IPCountry` / `X-Country-Code` / `X-Geo-Country`
   at the edge, else the `country` field. `UA→uk`, `DE/AT/CH→de`.
3. `Accept-Language`.
4. English.

The chosen language is **stored on the request row**, so the confirmation weeks
later matches the acknowledgement even if the visitor's browser has changed.

Templates exist for `en`, `de`, `uk` only. A visitor on the Polish page falls
through to their country, then to English.

---

## Switching it on in production

### 1. The mailbox credential — this is the step that catches people

`klarnote.com` is Google Workspace (`MX → smtp.google.com`). **Google has refused
plain account passwords for SMTP and IMAP since 2024.** The account password will
fail with:

```
535-5.7.8 Username and Password not accepted
```

You need an **App Password**:

1. Turn on 2-Step Verification for `sales@klarnote.com`.
2. Mint a 16-character App Password at <https://myaccount.google.com/apppasswords>.
3. Use that value for **both** `MDX_MARKETING_SMTP_PASSWORD` and
   `MDX_DEMO_IMAP_PASSWORD`.

Also confirm IMAP is enabled for the mailbox (Gmail → Settings → Forwarding and
POP/IMAP → Enable IMAP), and that the Workspace admin has not blocked App
Passwords org-wide (Admin console → Security → Less secure apps / App passwords).

### 2. Environment

```bash
MDX_EMAIL_PROVIDER=smtp
MDX_MARKETING_SMTP_HOST=smtp.gmail.com
MDX_MARKETING_SMTP_PORT=587          # 465 also works — the code picks implicit TLS there
MDX_MARKETING_SMTP_USE_TLS=true
MDX_MARKETING_SMTP_USERNAME=sales@klarnote.com
MDX_MARKETING_SMTP_PASSWORD=<app password>
MDX_MARKETING_EMAIL_FROM=sales@klarnote.com
MDX_MARKETING_REPLY_TO=sales@klarnote.com

MDX_DEMO_WATCH_ENABLED=true
MDX_DEMO_IMAP_HOST=imap.gmail.com
MDX_DEMO_IMAP_PORT=993
MDX_DEMO_IMAP_USERNAME=sales@klarnote.com
MDX_DEMO_IMAP_PASSWORD=<the same app password>

MDX_DEMO_BOOKING_URL=https://calendar.app.google/336HQrX67TV9UNJr8
MDX_PUBLIC_BASE_URL=https://klarnote.com
MDX_DEMO_IP_HASH_SALT=<long random string — NOT the dev default>
CORS_ALLOWED_ORIGINS=https://klarnote.com
```

`MDX_DEMO_WATCH_ENABLED=false` (the default) is a valid half-state: the
acknowledgement still goes out, bookings are simply never detected. The service
logs a WARNING at startup saying exactly that, so it cannot be mistaken for
working.

### 3. Deliverability

Both mails come from `sales@klarnote.com` through Google's own relay, so SPF and
DKIM are already aligned if the domain is set up normally. Verify before the
first campaign:

```bash
dig +short TXT klarnote.com | grep spf          # must include _spf.google.com
dig +short TXT google._domainkey.klarnote.com   # DKIM must be published
dig +short TXT _dmarc.klarnote.com              # publish at least p=none
```

The mails carry `List-Unsubscribe` + `List-Unsubscribe-Post` (RFC 8058).

### 4. Two public paths the emails assume exist

Both are built from `MDX_PUBLIC_BASE_URL`, and **neither is routed yet**. They
degrade differently, which is why only one of them is a launch blocker:

| Path | Used by | If missing |
|---|---|---|
| `/unsubscribe?t=<token>` | `List-Unsubscribe` on both mails | **Blocker.** Gmail and Yahoo require a working unsubscribe on bulk mail; a header pointing at a 404 is worse than no header |
| `/api/marketing/public/demo/<token>.ics` | the "Download .ics" link in the confirmation | Degraded only — the same calendar file is *attached* to the mail, so the reader still gets it |

The `.ics` route needs the production gateway to forward `/api/marketing/*` to
marketing-service, stripping the prefix. Until it does, point
`MDX_PUBLIC_BASE_URL` at whatever origin *does* reach the service, or accept the
dead link and rely on the attachment.

---

## Guards on the booking watcher

Two, and both default to **not** sending. Every other meeting on the sales
calendar also arrives as an invitation, and mailing those attendees a Klarnote
demo confirmation is worse than missing a booking a human can chase.

| Guard | Setting | Default |
|---|---|---|
| Event summary must contain this | `MDX_DEMO_SUMMARY_MATCH` | `demo` |
| Confirm bookings with no matching request | `MDX_DEMO_CONFIRM_UNMATCHED` | `false` |

A third guard is structural and not configurable: an invitation with more than
one non-host attendee is treated as a hand-made meeting, not a booking.

If your appointment schedule is not named something containing "demo", either
rename it or change `MDX_DEMO_SUMMARY_MATCH` — otherwise **no confirmation will
ever be sent** and the only symptom is a rising
`mdx_demo_bookings_unmatched_total`.

---

## Idempotency

- `marketing.demo_bookings (ical_uid, sequence)` is UNIQUE. The IMAP poll window
  overlaps by design, so the same invitation is re-read constantly; the unique
  index is what stops that becoming a confirmation every two minutes.
- A **reschedule** bumps Google's `SEQUENCE`, so it inserts a new row and
  correctly re-sends with the new time.
- A **cancellation** is recorded (the `.ics` endpoint stops serving the dead
  slot) but sends no mail — Google already told the prospect.
- Partial unique indexes on the outbox cap it at one acknowledgement per request
  and one confirmation per booking, regardless of how many callers enqueue.

---

## Monitoring

| Metric | Meaning |
|---|---|
| `mdx_demo_requests_total` | Requests accepted |
| `mdx_demo_bookings_detected_total` | Invitations recognised as bookings |
| `mdx_demo_bookings_unmatched_total` | Bookings with no request — see guards above |
| `mdx_demo_mail_dead_lettered_total` | **Alert on any increase.** A prospect asked to see the product and heard nothing |
| `mdx_demo_booking_watch_failures_total` | Sustained non-zero = the mailbox is unreachable and bookings are being missed |

Dead letters are visible in SQL too:

```sql
SELECT kind, to_address, attempt_count, last_error
FROM marketing.demo_mail_outbox
WHERE status = 'dead'
ORDER BY created_at DESC;
```

---

## Triage

**"I submitted the form and got nothing."**

```sql
SELECT id, email, lang, status, requested_at FROM marketing.demo_requests
WHERE lower(email) = lower('...') ORDER BY requested_at DESC LIMIT 5;

SELECT kind, status, attempt_count, next_attempt_at, last_error
FROM marketing.demo_mail_outbox WHERE request_id = '...';
```

- No request row → the browser call never landed. Check CORS and the SPA's
  `VITE_MARKETING_SERVICE_URL`, and whether the per-IP hourly cap silently
  swallowed it (`demo.rate_limited` in the logs — the endpoint answers 202
  either way, on purpose).
- Outbox row `pending` with a rising `attempt_count` → SMTP is refusing. Read
  `last_error`; `535` means the App Password (see above).
- Outbox row `sent` → it left us. Check spam and the DMARC report.

**"They booked but got no confirmation."**

```sql
SELECT ical_uid, sequence, attendee_email, starts_at, cancelled
FROM marketing.demo_bookings ORDER BY detected_at DESC LIMIT 10;
```

- No booking row → the watcher never saw it. Check `MDX_DEMO_WATCH_ENABLED`, the
  IMAP credential, and whether the event summary matches
  `MDX_DEMO_SUMMARY_MATCH`.
- Booking row but no outbox row → it was matched to no request and
  `MDX_DEMO_CONFIRM_UNMATCHED` is off. Expected when someone books directly
  from a link rather than through the form.

**Re-send a confirmation by hand:**

```sql
UPDATE marketing.demo_mail_outbox
SET status = 'pending', next_attempt_at = now(), attempt_count = 0
WHERE id = '...';
```

---

## Local development

```bash
make dev-up && make migrate-up
make run-marketing-service        # :8012

curl -X POST http://localhost:8012/public/demo/requests \
  -H 'Content-Type: application/json' \
  -d '{"email":"you@example.test","lang":"de"}'
```

The mail lands in Mailpit at <http://localhost:8025>. The booking watcher is off
in dev — there is no Google mailbox to watch — so the confirmation has to be
exercised through `jobs.booking_watch.handle_invite` in a test, which
`tests/unit/test_ics_parse.py` does against a real Google invitation fixture.
