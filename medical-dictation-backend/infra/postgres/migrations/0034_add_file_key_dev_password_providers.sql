-- S09-revision — new signing providers: file_key + dev_password.
--
-- ``file_key``     — server-side signing with the clinician's own КНЕДП
--                    file key container (qualified tier).
-- ``dev_password`` — development-only account-password confirmation
--                    scaffold (dev tier; triple-guarded against
--                    production in signing-service config, provider
--                    constructor, and the CI grep gate).
--
-- ALTER TYPE ... ADD VALUE is transaction-safe on PostgreSQL 12+ as long
-- as the new label is not *used* in the same transaction. The CHECK
-- constraints that reference these labels live in migration 0035 for
-- exactly that reason.

ALTER TYPE signing_provider ADD VALUE IF NOT EXISTS 'file_key';
ALTER TYPE signing_provider ADD VALUE IF NOT EXISTS 'dev_password';
