-- 0079_marketing_subscribe_mail.sql — the newsletter confirmation.
--
-- Third letter from the same funnel: "book a demo" sends people to a calendar,
-- "contact us" promises a human, and now "subscribe" confirms a subscription
-- and says what will arrive. `kind` is CHECKed, so a new template is a
-- migration.
--
-- The request row is unchanged, for the same reason 0078 gave: a subscription
-- IS an email address a stranger gave us, `source_page` records which form it
-- came from, and only the letter differs. A subscribers table with its own
-- lifecycle is the right shape once there is a newsletter to send — see the
-- note in the FE footer.
BEGIN;

ALTER TABLE marketing.demo_mail_outbox
    DROP CONSTRAINT demo_mail_outbox_kind_check;

ALTER TABLE marketing.demo_mail_outbox
    ADD CONSTRAINT demo_mail_outbox_kind_check
    CHECK (kind IN ('request_received', 'demo_confirmed', 'contact_received', 'subscribe_confirmed'));

-- One confirmation per request. A double submit must not send a real person
-- two "you're subscribed" letters.
CREATE UNIQUE INDEX demo_mail_outbox_one_subscribe_ack_idx
    ON marketing.demo_mail_outbox (request_id)
    WHERE kind = 'subscribe_confirmed';

COMMIT;
