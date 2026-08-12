-- 0080_marketing_contact_sales_notice.sql — the contact form's message reaches
-- a human mailbox, not only the CRM.
--
-- THE GAP THIS CLOSES. The contact acknowledgement (migration 0078) tells the
-- visitor "a manager reads it — not an autoresponder queue". That was true only
-- if someone was watching HubSpot: the message text went to the CRM and nowhere
-- else, because `demo_requests` had no column for it and the acknowledgement
-- template never showed it. This adds the column, and a second outbox kind that
-- forwards the original message to the sales mailbox.
--
-- WHY STORE THE MESSAGE AT ALL, when the outbox row would carry it in
-- render_fields anyway: an outbox row is deleted once drained in the normal
-- retention sweep, and a sales notice that dead-letters leaves nothing behind
-- to resend from. The request row is the record of what the visitor actually
-- wrote. It is prospect data under the same 24-month retention as the rest of
-- the row — see the Privacy Policy §3 "Prospect data" and §8.
BEGIN;

-- Free text from an unauthenticated form. Length-capped in the API model
-- (routers/demo.py) rather than here: a CHECK that rejects at the database is
-- a 500 to the visitor, where the model's cap is a clean 422 and, for the
-- message field, a truncation the SPA does before sending.
ALTER TABLE marketing.demo_requests
    ADD COLUMN IF NOT EXISTS message TEXT,
    ADD COLUMN IF NOT EXISTS reason  TEXT;

COMMENT ON COLUMN marketing.demo_requests.message IS
    'The contact form''s free-text message, verbatim. NULL for demo and subscribe submissions, which have no message field.';
COMMENT ON COLUMN marketing.demo_requests.reason IS
    'The contact form''s reason dropdown, stored as its English value so it reads the same whichever language the form was in.';

ALTER TABLE marketing.demo_mail_outbox
    DROP CONSTRAINT demo_mail_outbox_kind_check;

ALTER TABLE marketing.demo_mail_outbox
    ADD CONSTRAINT demo_mail_outbox_kind_check
    CHECK (kind IN (
        'request_received',
        'demo_confirmed',
        'contact_received',
        'subscribe_confirmed',
        -- The only kind addressed to us rather than to the visitor. It carries
        -- the original message to the sales mailbox and sets Reply-To to the
        -- sender, so answering it is one keystroke.
        'contact_internal'
    ));

-- One notice per request, the same guarantee the two acknowledgements have and
-- for the same reason: a double submit or a retry must not put two copies of
-- the same enquiry in the sales mailbox, where the second reads as a second
-- prospect.
CREATE UNIQUE INDEX demo_mail_outbox_one_contact_notice_idx
    ON marketing.demo_mail_outbox (request_id)
    WHERE kind = 'contact_internal';

COMMIT;
