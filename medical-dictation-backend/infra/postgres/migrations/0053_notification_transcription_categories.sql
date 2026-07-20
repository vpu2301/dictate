-- Sprint 12 follow-up — admit the `transcription.*` categories.
--
-- See 0052 for why this migration is mandatory rather than cosmetic: the
-- CHECK is the only one of the three places pinning this vocabulary that
-- a RUNNING system enforces, so an enum member without a matching
-- constraint turns every event of the new category into a
-- CheckViolationError inside the consumer, then a DLQ entry, and the
-- user sees the same nothing that motivated the change.

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
            'system.digest'
        ));
