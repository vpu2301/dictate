-- Reverses 0079. Refuses while confirmations exist: the narrowed CHECK cannot
-- be added over a violating row, and deleting the record of mail sent to a real
-- person is not something a migration should do quietly.
BEGIN;

DROP INDEX IF EXISTS marketing.demo_mail_outbox_one_subscribe_ack_idx;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM marketing.demo_mail_outbox WHERE kind = 'subscribe_confirmed') THEN
        RAISE EXCEPTION
            'subscribe_confirmed rows exist in marketing.demo_mail_outbox — archive or delete them before reverting 0079';
    END IF;
END $$;

ALTER TABLE marketing.demo_mail_outbox
    DROP CONSTRAINT demo_mail_outbox_kind_check;

ALTER TABLE marketing.demo_mail_outbox
    ADD CONSTRAINT demo_mail_outbox_kind_check
    CHECK (kind IN ('request_received', 'demo_confirmed', 'contact_received'));

COMMIT;
