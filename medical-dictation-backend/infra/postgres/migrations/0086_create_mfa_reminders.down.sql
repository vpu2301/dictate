-- Reverse of 0086_create_mfa_reminders.sql.
--
-- Open findings are lost with the table; there is nowhere else to keep them.
-- The audit trail is unaffected — every `user.mfa_reminded` event stays in
-- `audit_events`, which is append-only and never touched by a rollback.

DROP TABLE IF EXISTS mfa_reminders;
