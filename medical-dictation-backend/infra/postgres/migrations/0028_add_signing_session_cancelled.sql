-- M1 / B2 — add the 'cancelled' terminal state to signing_session_status.
--
-- A user may abort an in-flight local/remote signing attempt before the
-- provider callback lands. DELETE /signing/sessions/{id} transitions a
-- session in {initiating, awaiting_user} → cancelled (failure_reason
-- 'user_cancelled'). Never cancellable once verifying/signed.
--
-- ALTER TYPE ... ADD VALUE is transaction-safe on PostgreSQL 12+ as long
-- as the new label is not *used* in the same transaction (it is not here).
-- IF NOT EXISTS keeps the apply idempotent across re-runs.

ALTER TYPE signing_session_status ADD VALUE IF NOT EXISTS 'cancelled';
