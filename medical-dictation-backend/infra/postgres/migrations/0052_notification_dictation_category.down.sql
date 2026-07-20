-- Reverse of 0052_notification_dictation_category.sql.
--
-- Narrowing a CHECK is not automatically safe: rows written while the
-- wider constraint was in force would fail validation. They are deleted
-- first — a notification is a derived, replayable artefact (the events
-- stream is the record), so dropping the ones whose category no longer
-- exists is the correct reversal, not data loss.

DELETE FROM notification_preferences WHERE category = 'dictation.completed';
DELETE FROM notifications WHERE category = 'dictation.completed';

ALTER TABLE notifications
    DROP CONSTRAINT notifications_category_check;

ALTER TABLE notifications
    ADD CONSTRAINT notifications_category_check
        CHECK (category IN (
            'report.finalized',
            'report.signed',
            'report.signing_failed',
            'report.amended',
            'report.chain_failure',
            'report.shared_with_you',
            'system.digest'
        ));

ALTER TABLE notification_preferences
    DROP CONSTRAINT notification_preferences_category_check;

ALTER TABLE notification_preferences
    ADD CONSTRAINT notification_preferences_category_check
        CHECK (category IN (
            'report.finalized',
            'report.signed',
            'report.signing_failed',
            'report.amended',
            'report.chain_failure',
            'report.shared_with_you',
            'system.digest'
        ));
