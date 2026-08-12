-- Reverses 0080. Refuses while notices exist, for the reason 0079's down gives:
-- the narrowed CHECK cannot be added over a violating row, and deleting the
-- record of mail already sent is not something a migration does quietly.
--
-- The two columns are dropped, and that DOES destroy the stored enquiries. It
-- is the only honest reversal — leaving an orphan column behind would make a
-- re-apply of 0080 silently inherit data from a schema version that no longer
-- claims to hold it — so the check above is what stands between this and a
-- production accident.
BEGIN;

DROP INDEX IF EXISTS marketing.demo_mail_outbox_one_contact_notice_idx;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM marketing.demo_mail_outbox WHERE kind = 'contact_internal') THEN
        RAISE EXCEPTION
            'contact_internal rows exist in marketing.demo_mail_outbox — archive or delete them before reverting 0080';
    END IF;
    IF EXISTS (SELECT 1 FROM marketing.demo_requests WHERE message IS NOT NULL) THEN
        RAISE EXCEPTION
            'marketing.demo_requests.message holds enquiries — export them before reverting 0080';
    END IF;
END $$;

ALTER TABLE marketing.demo_mail_outbox
    DROP CONSTRAINT demo_mail_outbox_kind_check;

ALTER TABLE marketing.demo_mail_outbox
    ADD CONSTRAINT demo_mail_outbox_kind_check
    CHECK (kind IN (
        'request_received', 'demo_confirmed', 'contact_received', 'subscribe_confirmed'
    ));

ALTER TABLE marketing.demo_requests
    DROP COLUMN IF EXISTS message,
    DROP COLUMN IF EXISTS reason;

COMMIT;
