-- Reverse of 0065_create_patient_documents.sql. The MinIO objects are NOT
-- touched here: dropping a table is a schema operation, and orphaning
-- ciphertext is safer than a migration that deletes patient files.
DROP TABLE IF EXISTS patient_documents;
