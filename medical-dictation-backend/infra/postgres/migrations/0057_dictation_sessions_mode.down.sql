DROP INDEX IF EXISTS dictation_sessions_mode_idx;
ALTER TABLE dictation_sessions
    DROP CONSTRAINT IF EXISTS dictation_sessions_conversation_has_encounter;
ALTER TABLE dictation_sessions DROP COLUMN IF EXISTS mode;
