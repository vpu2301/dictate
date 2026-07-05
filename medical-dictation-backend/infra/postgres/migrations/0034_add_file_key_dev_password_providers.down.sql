-- Down: no-op (documented).
--
-- PostgreSQL has no DROP VALUE for an enum; removing a label requires
-- recreating the type and rewriting every dependent column/default — far
-- too invasive to do automatically and unsafe if any row already uses
-- 'file_key' / 'dev_password'. Rolling this migration back therefore
-- leaves the labels in place. This matches the repo convention for
-- additive enum migrations (see 0028).
SELECT 1;
