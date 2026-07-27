DROP INDEX IF EXISTS encounters_open_idx;

ALTER TABLE encounters DROP CONSTRAINT IF EXISTS encounters_ended_has_ts;

-- Any row parked in the status this migration introduced has to land
-- somewhere the 0031 CHECK admits; a paused visit is an open visit.
UPDATE encounters SET status = 'in_progress' WHERE status = 'paused';

ALTER TABLE encounters DROP CONSTRAINT IF EXISTS encounters_status_check;
ALTER TABLE encounters
    ADD CONSTRAINT encounters_status_check
        CHECK (status IN ('scheduled', 'in_progress', 'completed', 'cancelled'));

ALTER TABLE encounters
    DROP COLUMN IF EXISTS started_at,
    DROP COLUMN IF EXISTS ended_at,
    DROP COLUMN IF EXISTS updated_at;
