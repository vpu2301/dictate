-- 0078_marketing_contact_mail.sql — the contact form's own acknowledgement.
--
-- The public site has two forms that end in an email from marketing-service:
-- "book a demo" (which sends someone to the calendar) and "contact us" (which
-- promises a human will reply, and offers the calendar as an alternative to
-- waiting). They are different letters, so they are different template kinds,
-- and `kind` is CHECKed — hence a migration rather than a code-only change.
--
-- The demo_requests row is unchanged: a contact submission IS a request from a
-- stranger with an email address, and `source_page` already records which form
-- it came from. Only the letter differs.
BEGIN;

ALTER TABLE marketing.demo_mail_outbox
    DROP CONSTRAINT demo_mail_outbox_kind_check;

ALTER TABLE marketing.demo_mail_outbox
    ADD CONSTRAINT demo_mail_outbox_kind_check
    CHECK (kind IN ('request_received', 'demo_confirmed', 'contact_received'));

-- One acknowledgement per request, same guarantee the demo ack has and for the
-- same reason: the enqueue path must not be able to send a real person two
-- copies because of a double submit or a retry.
CREATE UNIQUE INDEX demo_mail_outbox_one_contact_ack_idx
    ON marketing.demo_mail_outbox (request_id)
    WHERE kind = 'contact_received';

COMMIT;
