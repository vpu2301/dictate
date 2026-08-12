-- Reverses 0078. Any queued or sent contact acknowledgements must go first:
-- the narrowed CHECK cannot be added while a row violates it, and silently
-- deleting a record of mail we sent to a real person is not something a
-- migration should do quietly — so this fails loudly if any exist.
BEGIN;

DROP INDEX IF EXISTS marketing.demo_mail_outbox_one_contact_ack_idx;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM marketing.demo_mail_outbox WHERE kind = 'contact_received') THEN
        RAISE EXCEPTION
            'contact_received rows exist in marketing.demo_mail_outbox — archive or delete them before reverting 0078';
    END IF;
END $$;

ALTER TABLE marketing.demo_mail_outbox
    DROP CONSTRAINT demo_mail_outbox_kind_check;

ALTER TABLE marketing.demo_mail_outbox
    ADD CONSTRAINT demo_mail_outbox_kind_check
    CHECK (kind IN ('request_received', 'demo_confirmed'));

COMMIT;
