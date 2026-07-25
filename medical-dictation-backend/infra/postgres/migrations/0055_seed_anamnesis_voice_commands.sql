-- 0055: anamnesis voice commands — sprint 13 step 07.
--
-- Two parts:
--   1. ``is_option_command`` — the flag that tells the sprint-05 FSM a
--      phrase is followed by an OPTION name to resolve against the
--      template's choice sections (the analogue of
--      ``is_section_command``).
--   2. The anamnesis command rows themselves.
--
-- SEEDING NOTE (as-built correction). The sprint-13 plan assumed
-- migration 0011 seeds the command catalogue. It does not — 0011 only
-- CREATEs the table; the rows come from
-- ``infra/postgres/seed/voice_commands_{uk,en}.json`` via
-- ``scripts/seed/seed_voice_commands.py`` (``make seed-voice-commands``),
-- which DELETEs every row for a language before re-inserting.
--
-- So the JSON fixtures are authoritative and carry these same commands.
-- The inserts below are an idempotent convenience for environments that
-- only run migrations: re-running the seeder converges to exactly the
-- same rows. A test asserts the two sources stay identical.

BEGIN;

ALTER TABLE voice_commands
    ADD COLUMN IF NOT EXISTS is_option_command BOOLEAN NOT NULL DEFAULT FALSE;

-- Disables the FSM's 1-substitution tolerance for a single spec. Needed
-- wherever a near-miss fires the OPPOSITE action: "прибрати" (remove) is
-- Levenshtein-2 from "обрати" (set). Also applied to the sprint-05
-- `slash` spec, where "слеш" was matching the very common clinical word
-- "слід" and inserting a stray "/" into notes.
ALTER TABLE voice_commands
    ADD COLUMN IF NOT EXISTS exact_match_only BOOLEAN NOT NULL DEFAULT FALSE;

UPDATE voice_commands SET exact_match_only = TRUE
WHERE intent = 'slash' AND language = 'uk';

COMMENT ON COLUMN voice_commands.is_option_command IS
    'Sprint 13: the phrase is followed by an option name resolved against '
    'the template''s choice/multi_choice sections. Mutually exclusive with '
    'is_section_command.';

-- Idempotent: skip any (intent, language) already present, so a DB that
-- was seeded from JSON first is left untouched.
INSERT INTO voice_commands
    (intent, language, phrases, requires_pause_before_ms,
     min_avg_probability, is_section_command, is_option_command,
     exact_match_only)
SELECT v.intent, v.language, v.phrases::jsonb, v.pause_ms,
       v.min_p, FALSE, v.is_option, TRUE
FROM (VALUES
    -- uk ────────────────────────────────────────────────────────────
    ('choice.set', 'uk',
     '[["обрати"],["вибрати"],["встановити"]]', 250, 0.85, TRUE),
    ('choice.add', 'uk',
     '[["додати"]]', 250, 0.85, TRUE),
    ('choice.remove', 'uk',
     '[["прибрати"],["видалити"]]', 250, 0.85, TRUE),
    ('diagnosis.capture', 'uk',
     '[["діагноз"],["основний","діагноз"]]', 300, 0.88, FALSE),
    -- en ────────────────────────────────────────────────────────────
    ('choice.set', 'en',
     '[["select"],["choose"],["set"]]', 250, 0.85, TRUE),
    ('choice.add', 'en',
     '[["add"]]', 250, 0.85, TRUE),
    ('choice.remove', 'en',
     '[["remove"],["delete"]]', 250, 0.85, TRUE),
    ('diagnosis.capture', 'en',
     '[["diagnosis"],["primary","diagnosis"]]', 300, 0.88, FALSE)
) AS v(intent, language, phrases, pause_ms, min_p, is_option)
WHERE NOT EXISTS (
    SELECT 1 FROM voice_commands vc
    WHERE vc.intent = v.intent AND vc.language = v.language
);

COMMIT;
