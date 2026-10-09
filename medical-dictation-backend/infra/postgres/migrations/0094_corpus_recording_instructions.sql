-- 0094: recording instructions live on the replica — corpus-v3 Epic C.
--
-- THE DEFECT. Until now the recording condition was a dropdown on the
-- SESSION: the operator picked "headset" once and every replica recorded
-- after that inherited it. Nothing checked whether the replica in front of
-- them was supposed to be recorded that way, and nothing told them what
-- "phone_noise" is supposed to sound like. So the corpus accumulated
-- utterances filed under a condition they were not recorded in, and the
-- per-condition WER table in the report was comparing labels rather than
-- setups.
--
-- THE FIX HAS THREE PARTS AND THIS MIGRATION HOLDS TWO OF THEM.
--
-- 1. The instruction is DATA, assembled server-side. A replica already
--    carries its condition; what it lacked was the sentence telling a human
--    what to do about it. Putting that sentence in the SPA would mean two
--    copies of the recording protocol drifting apart, and the one that
--    matters — the one the recordist reads — would be the one nobody
--    reviews. So the server composes "condition sentence + category
--    sentence" from this table and serves the finished text.
--
-- 2. A take must be CONFIRMED against its replica's condition. The
--    dropdown becomes a statement of fact ("this is how I actually recorded
--    it") rather than a setting, and a take saved without that statement is
--    not "recorded" (Epic C's acceptance criterion). Where the take's
--    condition differs from the replica's, the attempt journal records
--    `condition_mismatch` — an override is legitimate, silently pretending
--    it did not happen is not.
--
-- WHY EXISTING TAKES BACKFILL TO condition_confirmed = true. They were
-- recorded under the old rule, and the rule cannot be applied retroactively
-- to a human who was never asked. Defaulting them to false would sweep the
-- entire existing corpus into the Epic E retake queue on the first read,
-- which would bury the takes that genuinely need re-recording.
--
-- WHY THE TEMPLATES ARE OVERRIDABLE PER TENANT. The seeded protocol is the
-- one this corpus was designed around, but a site with a different phone or
-- a noisier clinic needs to say so, and telling them to edit a Python module
-- is how the corpus stayed at 34 lines for a sprint. Same PERMISSIVE
-- system/tenant split as medical_synonyms (0063): system rows are visible to
-- everyone and writable by nobody through app_role, tenant rows belong to
-- their tenant.

BEGIN;

-- ── the instruction dictionary ────────────────────────────────────────

CREATE TABLE corpus_eval_instruction_templates (
    id         uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    scope      text        NOT NULL CHECK (scope IN ('system', 'tenant')),
    tenant_id  uuid        REFERENCES tenants(id) ON DELETE CASCADE,
    -- A row is EITHER the condition half of an instruction or the category
    -- half; the served text is the two concatenated. Splitting them is what
    -- keeps the table at 4 + 7 rows instead of 4 × 7 rows that drift.
    condition  text
        CONSTRAINT eval_instr_condition_chk CHECK (
            condition IS NULL OR condition IN
            ('headset', 'laptop-mic', 'phone-speaker-distance', 'noisy')),
    -- Epic C calls this "category"; the value is the corpus's own subset
    -- vocabulary, plus 'baseline' for a line with no subset. Same words the
    -- rest of the pipeline reports by — a second naming would need a second
    -- mapping, and mappings are where categories go to get mismatched.
    category   text
        CONSTRAINT eval_instr_category_chk CHECK (
            category IS NULL OR category IN
            ('baseline', 'numbers_doses_units', 'drug_names', 'abbreviations',
             'code_switching', 'voice_commands', 'phone_mic_noisy')),
    lang_ui    text        NOT NULL CHECK (lang_ui IN ('uk', 'en', 'de')),
    text       text        NOT NULL CHECK (char_length(text) BETWEEN 1 AND 400),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT eval_instr_one_axis_chk
        CHECK ((condition IS NULL) <> (category IS NULL)),
    CONSTRAINT eval_instr_scope_chk
        CHECK ((scope = 'system') = (tenant_id IS NULL))
);

-- NULL is not equal to NULL in a UNIQUE constraint, so the two axes get one
-- partial index each rather than a single composite that would silently
-- permit duplicates.
CREATE UNIQUE INDEX corpus_eval_instr_system_condition_idx
    ON corpus_eval_instruction_templates (condition, lang_ui)
    WHERE scope = 'system' AND condition IS NOT NULL;
CREATE UNIQUE INDEX corpus_eval_instr_system_category_idx
    ON corpus_eval_instruction_templates (category, lang_ui)
    WHERE scope = 'system' AND category IS NOT NULL;
CREATE UNIQUE INDEX corpus_eval_instr_tenant_condition_idx
    ON corpus_eval_instruction_templates (tenant_id, condition, lang_ui)
    WHERE scope = 'tenant' AND condition IS NOT NULL;
CREATE UNIQUE INDEX corpus_eval_instr_tenant_category_idx
    ON corpus_eval_instruction_templates (tenant_id, category, lang_ui)
    WHERE scope = 'tenant' AND category IS NOT NULL;

ALTER TABLE corpus_eval_instruction_templates ENABLE ROW LEVEL SECURITY;
ALTER TABLE corpus_eval_instruction_templates FORCE ROW LEVEL SECURITY;

CREATE POLICY eval_instr_visibility ON corpus_eval_instruction_templates
    FOR SELECT TO app_role
    USING (
        scope = 'system'
        OR tenant_id = current_setting('app.tenant_id', true)::uuid
    );
-- System rows have no PERMISSIVE write policy at all, which IS the guard.
CREATE POLICY eval_instr_tenant_insert ON corpus_eval_instruction_templates
    FOR INSERT TO app_role
    WITH CHECK (
        scope = 'tenant'
        AND tenant_id = current_setting('app.tenant_id', true)::uuid
    );
CREATE POLICY eval_instr_tenant_update ON corpus_eval_instruction_templates
    FOR UPDATE TO app_role
    USING (scope = 'tenant' AND tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (scope = 'tenant' AND tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY eval_instr_tenant_delete ON corpus_eval_instruction_templates
    FOR DELETE TO app_role
    USING (scope = 'tenant' AND tenant_id = current_setting('app.tenant_id', true)::uuid);

GRANT SELECT, INSERT, UPDATE, DELETE ON corpus_eval_instruction_templates TO app_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON corpus_eval_instruction_templates TO tenant_writer;
CREATE POLICY eval_instr_seed_select ON corpus_eval_instruction_templates
    FOR SELECT TO tenant_writer USING (true);

-- ── the seeded protocol (Epic C's starter set) ────────────────────────

INSERT INTO corpus_eval_instruction_templates (scope, condition, lang_ui, text)
VALUES
    ('system', 'headset', 'uk',
     'Запис у гарнітурі. Тихе приміщення, мікрофон за два–три сантиметри від рота, природний темп мовлення.'),
    ('system', 'phone-speaker-distance', 'uk',
     'Запис із телефона на відстані близько метра. Увімкніть фоновий шум (радіо або розмова). Телефон до рота не підносити.'),
    ('system', 'laptop-mic', 'uk',
     'Запис вбудованим мікрофоном ноутбука. Ноутбук на столі, руки від корпусу прибрати, говоріть у звичайній позі.'),
    ('system', 'noisy', 'uk',
     'Запис у шумі. Увімкніть фоновий шум (радіо, розмова або коридор) так, щоб він був чутний, але мовлення лишалося розбірливим.');

INSERT INTO corpus_eval_instruction_templates (scope, category, lang_ui, text)
VALUES
    ('system', 'baseline', 'uk',
     'Читайте спокійно, як звичайне диктування.'),
    ('system', 'numbers_doses_units', 'uk',
     'Числа промовляйте чітко, без пауз усередині дози.'),
    ('system', 'drug_names', 'uk',
     'Назви препаратів читайте точно як у тексті, не «виправляйте».'),
    ('system', 'abbreviations', 'uk',
     'Скорочення читайте так, як кажете їх у клініці.'),
    ('system', 'code_switching', 'uk',
     'Іншомовні вставки читайте природно, без наголошування.'),
    ('system', 'voice_commands', 'uk',
     'Команду прочитайте як фразу, не виконуйте її.'),
    ('system', 'phone_mic_noisy', 'uk',
     'Читайте у звичайному темпі — гучніше говорити не треба, шум є частиною виміру.');

-- ── the take must state the condition it was recorded in ──────────────

ALTER TABLE corpus_eval_takes
    -- "Так, я справді записав це в цій умові." Epic C: a take without it is
    -- not "recorded". See the header for why existing rows backfill to true.
    ADD COLUMN condition_confirmed boolean NOT NULL DEFAULT false;

UPDATE corpus_eval_takes SET condition_confirmed = true;

ALTER TABLE corpus_eval_take_attempts
    -- The take's condition differed from the replica's. An override with a
    -- reason is fine; an override nobody can find afterwards is how the
    -- per-condition table stopped meaning anything.
    ADD COLUMN condition_mismatch boolean NOT NULL DEFAULT false,
    -- What the replica ASKED for, so the journal is readable without
    -- joining against a script row that may since have been edited.
    ADD COLUMN expected_condition text
        CONSTRAINT eval_attempt_expected_condition_chk CHECK (
            expected_condition IS NULL OR expected_condition IN
            ('headset', 'laptop-mic', 'phone-speaker-distance', 'noisy'));

CREATE INDEX corpus_eval_take_attempts_mismatch_idx
    ON corpus_eval_take_attempts (tenant_id, script_id)
    WHERE condition_mismatch;

COMMIT;
