-- Down: narrow the language CHECKs back to ('uk','en').
--
-- Catalogue rows (prompts, commands, abbreviations) are deleted here —
-- they are seed data and reproducible. Recorded German dictation
-- sessions are NOT: the down ABORTS if any exist rather than deleting a
-- patient's session row to satisfy a constraint. Erase them through the
-- erasure engine first if the rollback is really intended.

BEGIN;

DO $$
DECLARE
    n BIGINT;
BEGIN
    SELECT count(*) INTO n FROM dictation_sessions WHERE language = 'de';
    IF n > 0 THEN
        RAISE EXCEPTION
            '0066 down: % German dictation session(s) exist; refusing to narrow the CHECK', n;
    END IF;
END $$;

DELETE FROM abbreviation_dictionary WHERE language = 'de';
DELETE FROM voice_commands WHERE language = 'de';
DELETE FROM medical_prompts WHERE language = 'de';

ALTER TABLE abbreviation_dictionary
    DROP CONSTRAINT IF EXISTS abbreviation_dictionary_language_check;
ALTER TABLE abbreviation_dictionary
    ADD CONSTRAINT abbreviation_dictionary_language_check
    CHECK (language IN ('uk','en'));

ALTER TABLE voice_commands
    DROP CONSTRAINT IF EXISTS voice_commands_language_check;
ALTER TABLE voice_commands
    ADD CONSTRAINT voice_commands_language_check
    CHECK (language IN ('uk','en'));

ALTER TABLE medical_prompts
    DROP CONSTRAINT IF EXISTS medical_prompts_language_check;
ALTER TABLE medical_prompts
    ADD CONSTRAINT medical_prompts_language_check
    CHECK (language IN ('uk','en'));

ALTER TABLE dictation_sessions
    DROP CONSTRAINT IF EXISTS dictation_sessions_language_check;
ALTER TABLE dictation_sessions
    ADD CONSTRAINT dictation_sessions_language_check
    CHECK (language IN ('uk','en'));

COMMIT;
