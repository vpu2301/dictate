-- Sprint 12 follow-up — admit `dictation.completed` as a category.
--
-- The category vocabulary is pinned in THREE places that must agree:
-- `notification_events.Category`, `notification_service.domain.catalog`
-- (a test asserts those two are 1:1), and these two CHECK constraints.
-- The constraints are the only one of the three that a running system
-- enforces, so adding an enum member without this migration turns every
-- event of the new category into a CheckViolationError inside the
-- consumer — the message retries, then lands in the DLQ, and the user
-- sees exactly the same nothing they saw before.
--
-- Rewritten rather than extended because Postgres has no "add value to
-- a CHECK IN list"; the constraint is dropped and recreated with the
-- full vocabulary. Both tables are validated against existing rows,
-- which is safe: the new list is a strict superset of the old one.

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
