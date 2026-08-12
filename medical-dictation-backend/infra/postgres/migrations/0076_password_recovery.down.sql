-- Rollback of 0076_password_recovery.
--
-- Dropping the tables destroys every outstanding reset and lockdown
-- token, which is the correct behaviour: rolling this migration back
-- removes the endpoints that would redeem them, so leaving the rows
-- would only preserve credentials nothing can spend.

DROP FUNCTION IF EXISTS public.tenants_with_due_auth_mail(INTEGER);
DROP FUNCTION IF EXISTS public.peek_password_reset_token(BYTEA, TEXT);
DROP FUNCTION IF EXISTS public.resolve_account_for_password_reset(TEXT);
DROP FUNCTION IF EXISTS public.consume_password_reset_token(BYTEA, TEXT);

DROP TABLE IF EXISTS auth_password_events;
DROP TABLE IF EXISTS auth_mail_outbox;
DROP TABLE IF EXISTS auth_password_reset_tokens;
