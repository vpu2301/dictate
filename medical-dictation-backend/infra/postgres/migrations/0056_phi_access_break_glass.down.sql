-- Reverse of 0056.
--
-- Dropping `phi_access_requests` destroys the record of every break-glass
-- read that ever happened. That is acceptable ONLY as a dev-stack
-- rollback: on any environment that has served real traffic, export the
-- table before running this — the audit chain holds the
-- `phi_access.granted` events, but the reason notes live only here.

-- Restore the pre-0056 category vocabulary (0053's list).
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

-- Rows of the now-unadmitted category would fail the restored CHECK.
DELETE FROM notifications WHERE category = 'phi_access.granted';

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

DROP TABLE IF EXISTS phi_access_requests;
DROP TABLE IF EXISTS auth_reauth_tickets;
