-- Reverse of 0087_notification_security_mfa_reminder.sql.
--
-- Rows of the withdrawn category have to go before the constraint can be
-- narrowed again (same as 0052/0053). The standing reminder is unaffected:
-- it lives in `mfa_reminders`, and the banner keeps rendering.

DELETE FROM notification_preferences WHERE category = 'security.mfa_reminder';
DELETE FROM notifications WHERE category = 'security.mfa_reminder';

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
            'transcription.completed',
            'transcription.failed',
            'phi_access.granted',
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
            'transcription.completed',
            'transcription.failed',
            'phi_access.granted',
            'system.digest'
        ));
