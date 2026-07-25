-- 0054 down: drop the ICD-10 reference table.
BEGIN;
DROP TABLE IF EXISTS icd10_codes;
COMMIT;
