-- Content-scoped (the 0026 discipline): deletes exactly the seeded groups
-- by their deterministic UUID prefix, never a blanket source='system' —
-- later corpus additions survive this down-migration.
DELETE FROM medical_synonyms
WHERE source = 'system'
  AND group_id::text LIKE '00000000-0000-4000-a000-%';
