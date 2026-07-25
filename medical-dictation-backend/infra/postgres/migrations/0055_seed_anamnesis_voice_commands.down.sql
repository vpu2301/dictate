-- 0055 down: remove exactly the anamnesis command rows + the flag.
BEGIN;

DELETE FROM voice_commands
WHERE intent IN ('choice.set', 'choice.add', 'choice.remove', 'diagnosis.capture');

ALTER TABLE voice_commands DROP COLUMN IF EXISTS is_option_command;
ALTER TABLE voice_commands DROP COLUMN IF EXISTS exact_match_only;

COMMIT;
