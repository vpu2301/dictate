-- 0058: encounter lifecycle — a visit can now be started, paused, resumed,
-- ended and cancelled.
--
-- As built in 0031, `encounters.status` was effectively write-once at INSERT:
-- the CHECK admitted 'in_progress' but no code path ever moved a row out of
-- it, and there was no UPDATE anywhere in the tree. A visit opened from the
-- SPA ("Почати прийом" → status='in_progress') therefore stayed in_progress
-- forever, so the pipeline accumulated visits that were long over.
--
-- This migration gives the row the vocabulary the lifecycle needs:
--   * 'paused'    — the clinician stepped out; the visit is still open.
--   * started_at  — when the visit actually went live (occurred_at stays the
--                   scheduling/when-it-happened marker, untouched).
--   * ended_at    — when it reached a terminal state, guarded by the same
--                   biconditional CHECK pattern 0031 uses for
--                   clinical_notes_signed_has_ts / patient_consents_withdrawn_has_ts.
--   * updated_at  — last transition, for the "stale open visit" sweep.
--
-- Backfill: every pre-existing row is historical. Terminal rows get
-- ended_at = occurred_at so the new invariant holds; open rows get
-- started_at = occurred_at so duration maths has a floor.

ALTER TABLE encounters
    ADD COLUMN started_at TIMESTAMPTZ,
    ADD COLUMN ended_at   TIMESTAMPTZ,
    ADD COLUMN updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

-- Widen the status domain. The 0031 constraint is the implicit name Postgres
-- gives an inline column CHECK.
ALTER TABLE encounters DROP CONSTRAINT IF EXISTS encounters_status_check;
ALTER TABLE encounters
    ADD CONSTRAINT encounters_status_check
        CHECK (status IN ('scheduled', 'in_progress', 'paused',
                          'completed', 'cancelled'));

UPDATE encounters
   SET ended_at = occurred_at
 WHERE status IN ('completed', 'cancelled')
   AND ended_at IS NULL;

UPDATE encounters
   SET started_at = occurred_at
 WHERE status IN ('in_progress', 'completed')
   AND started_at IS NULL;

ALTER TABLE encounters
    ADD CONSTRAINT encounters_ended_has_ts
        CHECK ((status IN ('completed', 'cancelled')) = (ended_at IS NOT NULL));

-- "Which visits are still open?" — the query behind the pipeline view and
-- the stale-visit sweep. encounters_schedule_idx is partial on 'scheduled'
-- only and cannot serve it.
CREATE INDEX encounters_open_idx
    ON encounters (tenant_id, updated_at DESC)
    WHERE status IN ('in_progress', 'paused');

COMMENT ON COLUMN encounters.started_at IS
    'When the visit went live (status → in_progress the first time). NULL for scheduled/cancelled-before-start.';
COMMENT ON COLUMN encounters.ended_at IS
    'When the visit reached completed/cancelled. Enforced by encounters_ended_has_ts.';
