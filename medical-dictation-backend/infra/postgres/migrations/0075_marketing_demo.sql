-- Demo booking funnel — the two transactional emails a prospect gets.
--
-- Deliberately in its OWN `marketing` schema, not `public`. Everything in
-- `public` is tenant-scoped PHI territory whose RLS predicate reads
-- `app.tenant_id`; a marketing lead has no tenant and never will, so putting
-- these rows next to patients would either force a fake tenant id into every
-- insert or leave three tables in the tenant schema that the RLS gate has to
-- carry a permanent exemption for. A separate schema says the true thing:
-- this data is outside the tenancy model.
--
-- RLS is still enabled and FORCEd. There is no tenant column to scope by, so
-- the policies are `USING (true)` and the ROLE is the boundary — the same
-- role-boundary reasoning migration 0073 used for the tenant-less signing
-- callback surface. The value is that a future table added here inherits the
-- habit, and a mis-granted role gets no rows by default.
--
-- No PHI lands here by construction: a lead is a work email and an
-- organisation name, captured before any clinical system exists for them.

CREATE SCHEMA IF NOT EXISTS marketing;
GRANT USAGE ON SCHEMA marketing TO app_role;

-- ── The request ──────────────────────────────────────────────────────
--
-- Created when someone submits the "Book a demo" form. `token` is the
-- unguessable handle that appears in email links (booking page, unsubscribe)
-- — the row id is never exposed, so a leaked link cannot be walked into an
-- enumeration of every lead we hold.
CREATE TABLE marketing.demo_requests (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    token         TEXT NOT NULL UNIQUE,
    email         TEXT NOT NULL,
    -- The template language actually sent. Resolved server-side from the
    -- form's UI language and the request's country (see domain/language.py),
    -- and stored rather than re-derived: the confirmation mail must arrive in
    -- the same language as the request mail, even if the visitor's browser
    -- has since changed.
    lang          TEXT NOT NULL CHECK (lang IN ('en', 'de', 'uk')),
    -- ISO-3166-1 alpha-2, upper case. Nullable: an unknown country is an
    -- honest answer, and it falls back to English.
    country       TEXT CHECK (country ~ '^[A-Z]{2}$'),
    first_name    TEXT,
    organisation  TEXT,
    source_page   TEXT,
    status        TEXT NOT NULL DEFAULT 'requested'
                  CHECK (status IN ('requested', 'booked', 'cancelled')),
    requested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    booked_at     TIMESTAMPTZ,
    -- Salted hash, never the address itself — enough to rate-limit and to
    -- spot a flood, not enough to be a location record for a named person.
    ip_hash       TEXT,
    user_agent    TEXT
);

-- The booking watcher matches an inbound calendar invitation to a request by
-- attendee address, newest open request first.
CREATE INDEX demo_requests_email_idx
    ON marketing.demo_requests (lower(email), requested_at DESC);
CREATE INDEX demo_requests_status_idx
    ON marketing.demo_requests (status, requested_at DESC);

-- ── The booking ──────────────────────────────────────────────────────
--
-- One row per calendar event we observed on the sales mailbox.
--
-- (ical_uid, sequence) is UNIQUE and that uniqueness IS the idempotency: the
-- IMAP watcher re-reads a message whenever a poll overlaps a previous one, and
-- without this a prospect would get a fresh "your demo is booked" every two
-- minutes. Google bumps SEQUENCE on a reschedule, so a genuine time change
-- inserts a new row and correctly re-sends, while a re-read of the same
-- invitation collides and does not.
CREATE TABLE marketing.demo_bookings (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id     UUID REFERENCES marketing.demo_requests (id) ON DELETE SET NULL,
    ical_uid       TEXT NOT NULL,
    sequence       INTEGER NOT NULL DEFAULT 0,
    attendee_email TEXT NOT NULL,
    starts_at      TIMESTAMPTZ NOT NULL,
    ends_at        TIMESTAMPTZ NOT NULL,
    -- Event timezone as Google reported it (e.g. Europe/Kyiv). The email
    -- renders the slot in the prospect's local time, and UTC alone cannot do
    -- that without guessing.
    event_timezone TEXT,
    meet_url       TEXT,
    summary        TEXT,
    cancelled      BOOLEAN NOT NULL DEFAULT FALSE,
    detected_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (ical_uid, sequence)
);

CREATE INDEX demo_bookings_request_idx ON marketing.demo_bookings (request_id);

-- ── The outbox ───────────────────────────────────────────────────────
--
-- Same shape as the notification-service outbox (migration 0047) and for the
-- same reason: sending inline from the request handler means an SMTP hiccup
-- either loses the mail or fails the form submission, and both of those turn
-- a won lead into a lost one. The handler writes a row; a worker drains it
-- with exponential backoff and dead-letters what it cannot deliver.
CREATE TABLE marketing.demo_mail_outbox (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id          UUID REFERENCES marketing.demo_requests (id) ON DELETE CASCADE,
    booking_id          UUID REFERENCES marketing.demo_bookings (id) ON DELETE CASCADE,
    kind                TEXT NOT NULL
                        CHECK (kind IN ('request_received', 'demo_confirmed')),
    to_address          TEXT NOT NULL,
    lang                TEXT NOT NULL CHECK (lang IN ('en', 'de', 'uk')),
    -- The rendered template's variables, frozen at enqueue time. The worker
    -- re-renders from these rather than re-reading the request, so a retry
    -- three hours later sends exactly the mail the first attempt would have.
    render_fields       JSONB NOT NULL DEFAULT '{}'::jsonb,
    status              TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'sent', 'dead')),
    attempt_count       INTEGER NOT NULL DEFAULT 0,
    next_attempt_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_error          TEXT,
    provider_message_id TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at             TIMESTAMPTZ
);

-- At most one acknowledgement per request, and one confirmation per booking.
-- Enforced here rather than in the enqueue path because the enqueue path has
-- two callers (the HTTP handler and the booking watcher) and a duplicate
-- suppressed by a race in one of them is a second email to a real prospect.
CREATE UNIQUE INDEX demo_mail_outbox_one_ack_idx
    ON marketing.demo_mail_outbox (request_id)
    WHERE kind = 'request_received';
CREATE UNIQUE INDEX demo_mail_outbox_one_confirm_idx
    ON marketing.demo_mail_outbox (booking_id)
    WHERE kind = 'demo_confirmed';

CREATE INDEX demo_mail_outbox_due_idx
    ON marketing.demo_mail_outbox (next_attempt_at)
    WHERE status = 'pending';

-- ── RLS: role as the boundary ────────────────────────────────────────
ALTER TABLE marketing.demo_requests    ENABLE ROW LEVEL SECURITY;
ALTER TABLE marketing.demo_requests    FORCE  ROW LEVEL SECURITY;
ALTER TABLE marketing.demo_bookings    ENABLE ROW LEVEL SECURITY;
ALTER TABLE marketing.demo_bookings    FORCE  ROW LEVEL SECURITY;
ALTER TABLE marketing.demo_mail_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE marketing.demo_mail_outbox FORCE  ROW LEVEL SECURITY;

CREATE POLICY demo_requests_app ON marketing.demo_requests
    FOR ALL TO app_role USING (true) WITH CHECK (true);
CREATE POLICY demo_bookings_app ON marketing.demo_bookings
    FOR ALL TO app_role USING (true) WITH CHECK (true);
CREATE POLICY demo_mail_outbox_app ON marketing.demo_mail_outbox
    FOR ALL TO app_role USING (true) WITH CHECK (true);

GRANT SELECT, INSERT, UPDATE ON marketing.demo_requests    TO app_role;
GRANT SELECT, INSERT, UPDATE ON marketing.demo_bookings    TO app_role;
GRANT SELECT, INSERT, UPDATE ON marketing.demo_mail_outbox TO app_role;
