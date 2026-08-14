-- S21 — admit the `security.mfa_reminder` category.
--
-- Same reason as 0052/0053: the CHECK is the only one of the three places
-- pinning this vocabulary that a RUNNING system enforces, so a Category enum
-- member without a matching constraint turns every event of the new category
-- into a CheckViolationError inside the consumer, then a DLQ entry, and the
-- user hears nothing.
--
-- The banner in the SPA (fed by `mfa_reminders` via GET /auth/me) is the
-- standing half of the reminder; this category is the arriving half — the
-- bell, and an email for a user who is not in the app that day.

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
            'security.mfa_reminder',
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
            'security.mfa_reminder',
            'system.digest'
        ));
