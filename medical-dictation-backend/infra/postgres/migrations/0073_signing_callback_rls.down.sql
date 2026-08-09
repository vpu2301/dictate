-- Sprint 16 rollback — drop the callback policies; keep tenant_update
-- (removing it would re-break session transitions on fresh databases).
DROP POLICY IF EXISTS signing_sessions_callback_select ON signing_sessions;
DROP POLICY IF EXISTS signing_sessions_callback_update ON signing_sessions;
