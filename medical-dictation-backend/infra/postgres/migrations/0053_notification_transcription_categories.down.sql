-- Reverse of 0053_notification_transcription_categories.sql.
--
-- Rows in the categories being withdrawn are deleted first, for the same
-- reason as 0052: narrowing a CHECK validates existing rows, and a
-- notification is a derived, replayable artefact — the events stream is
-- the record of what happened.

DELETE FROM notification_preferences
    WHERE category IN ('transcription.completed', 'transcription.failed');
DELETE FROM notifications
    WHERE category IN ('transcription.completed', 'transcription.failed');

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
            'dictation.completed',
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
            'dictation.completed',
            'system.digest'
        ));
