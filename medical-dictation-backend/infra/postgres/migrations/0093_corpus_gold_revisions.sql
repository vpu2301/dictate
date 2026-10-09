-- 0093: the gold-transcript revision journal — corpus-v3 Epic B.
--
-- Epic B sets the house style for reference transcripts: they are written in
-- SPOKEN form. Numbers as words, no "/" and no "%", abbreviations as a
-- clinician says them. The reason is in the v2 measurement — 18.1% raw
-- against 14.4% normalised, and a large part of that gap was not the model
-- mishearing anything, it was the reference disagreeing with the model about
-- house style. "пульс 68/хв" against a correctly-heard "пульс шістдесят
-- вісім за хвилину" is 50% WER on a perfect transcription.
--
-- WHY EDITING A REFERENCE NEEDS A JOURNAL AT ALL. A corpus is the constant
-- a measurement is taken against. Change a gold transcript and every
-- previously stored WER for that utterance silently starts describing a
-- comparison that no longer exists — the number stays in the table looking
-- exactly as authoritative as it did yesterday. That is the failure mode
-- this table exists to make impossible: the change is recorded, and any run
-- that predates it is marked "еталон змінено" when it is read back.
--
-- The marking is DERIVED, never stored: a run item is stale if a revision
-- for its script_id is newer than the run's start. Deriving it cannot go out
-- of date, and it means no historical run row is ever rewritten — which
-- would be the same sin one level up.
--
-- WHY canonical_equal IS A COLUMN. Most of these revisions do not change
-- what is being measured: "140/90 мм рт. ст." and "сто сорок на дев'яносто
-- міліметрів ртутного стовпа" are the same utterance under the normaliser,
-- so the normalised WER is untouched and only the raw one moves. Some DO
-- change it: rewriting the abbreviation gold "АТ" as the spoken "А Те"
-- changes the abbreviations subset from "does the pipeline fold the letter
-- names" into "does the ASR hear the letter names" — a different question.
-- Both are legitimate; conflating them is not, so the server computes the
-- answer at revision time and stores it.
--
-- INSERT-ONLY, like the 0092 journals and for the same reason. An editable
-- record of what we edited answers a question nobody has.

BEGIN;

CREATE TABLE corpus_eval_gold_revisions (
    id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     uuid        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    script_id     text        NOT NULL
        CONSTRAINT eval_gold_rev_script_id_chk
        CHECK (script_id ~ '^[a-z0-9][a-z0-9_-]{0,119}$'),
    -- Which half of the corpus the line was in when it was revised. A test
    -- revision is the serious one: it changes the frozen holdout, which is
    -- why the API demands a separate confirmation for it.
    dataset       text        NOT NULL
        CONSTRAINT eval_gold_rev_dataset_chk CHECK (dataset IN ('dev', 'test')),
    old_transcript text       NOT NULL
        CHECK (char_length(old_transcript) BETWEEN 1 AND 500),
    new_transcript text       NOT NULL
        CHECK (char_length(new_transcript) BETWEEN 1 AND 500),
    -- style_migration — the Epic B sweep proposed it and a human accepted
    -- manual_edit     — an ordinary line edit that happened to change gold
    -- vendored_spine  — the repo's own script changed (eval_script.py)
    reason        text        NOT NULL
        CONSTRAINT eval_gold_rev_reason_chk
        CHECK (reason IN ('style_migration', 'manual_edit', 'vendored_spine')),
    -- The rules the equivalence below was decided under. Without it,
    -- canonical_equal is an assertion with no stated basis.
    normalizer_version text   NOT NULL,
    -- Do the old and new gold mean the same measurement? See the header.
    canonical_equal boolean   NOT NULL,
    -- users.sub, soft ref (0089 pattern). NULL for vendored_spine: nobody in
    -- this tenant made that change, a repository commit did.
    revised_by    uuid,
    -- clock_timestamp(), not now(): several revisions land in one apply
    -- transaction and the ordering between them is the journal's content.
    created_at    timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX corpus_eval_gold_revisions_line_idx
    ON corpus_eval_gold_revisions (tenant_id, script_id, created_at DESC);

CREATE INDEX corpus_eval_gold_revisions_recent_idx
    ON corpus_eval_gold_revisions (tenant_id, created_at DESC);

GRANT SELECT, INSERT ON corpus_eval_gold_revisions TO app_role;
ALTER TABLE corpus_eval_gold_revisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE corpus_eval_gold_revisions FORCE ROW LEVEL SECURITY;
CREATE POLICY eval_gold_revisions_tenant_all ON corpus_eval_gold_revisions
    FOR ALL TO app_role
    USING      (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

-- ── the vendored spine's own revision ─────────────────────────────────
--
-- eval_script.py ships 14 lines whose gold was written in the post-NLP style
-- this epic replaces. The module is repository data, so the rewrite is a
-- commit rather than an API call — but every tenant that already scored
-- those lines is affected exactly as if it had been one, and their runs must
-- carry the same "еталон змінено" mark. Seeding the journal here is what
-- makes the two paths indistinguishable to everything downstream.
--
-- canonical_equal is FALSE for the four abbreviation lines on purpose: those
-- revisions change the question the subset asks (see the header). The other
-- ten are pure formatting and the normalised score does not move.

INSERT INTO corpus_eval_gold_revisions
    (tenant_id, script_id, dataset, old_transcript, new_transcript,
     reason, normalizer_version, canonical_equal, revised_by)
SELECT t.id, v.script_id, 'test', v.old_gold, v.new_gold,
       'vendored_spine', 'v2', v.equal, NULL
FROM tenants t
CROSS JOIN (VALUES
    ('uk-numbers-001',
     'Артеріальний тиск 140/90 мм рт. ст., пульс 72 за хвилину.',
     'Артеріальний тиск сто сорок на дев''яносто міліметрів ртутного стовпа, пульс сімдесят два за хвилину.',
     true),
    ('uk-numbers-002',
     'Глікований гемоглобін 7,2 %.',
     'Глікований гемоглобін сім цілих дві десятих відсотка.',
     true),
    ('uk-numbers-003',
     'Призначено 25 мг двічі на добу протягом 14 днів.',
     'Призначено двадцять п''ять міліграмів двічі на добу протягом чотирнадцяти днів.',
     true),
    ('uk-numbers-004',
     'Температура тіла 37,8, частота дихання 18 за хвилину.',
     'Температура тіла тридцять сім і вісім, частота дихання вісімнадцять за хвилину.',
     true),
    ('uk-numbers-005',
     'Вузол діаметром 12 × 8 мм у нижній частці справа.',
     'Вузол діаметром дванадцять на вісім міліметрів у нижній частці справа.',
     true),
    ('en-numbers-001',
     'Blood pressure 138/84, heart rate 66 bpm.',
     'Blood pressure one thirty eight over eighty four, heart rate sixty six beats per minute.',
     true),
    ('uk-drugs-001',
     'Продовжити бісопролол 5 мг вранці та розувастатин 10 мг увечері.',
     'Продовжити бісопролол п''ять міліграмів вранці та розувастатин десять міліграмів увечері.',
     true),
    ('uk-drugs-002',
     'Метформін 1000 мг двічі на добу після їжі.',
     'Метформін тисяча міліграмів двічі на добу після їжі.',
     true),
    ('uk-drugs-003',
     'Пантопразол 40 мг натще, амоксицилін/клавуланова кислота 875/125.',
     'Пантопразол сорок міліграмів натще, амоксицилін клавуланова кислота вісімсот сімдесят п''ять на сто двадцять п''ять.',
     true),
    ('uk-noisy-002',
     'Артеріальний тиск 130/80, скарг на біль у грудях немає.',
     'Артеріальний тиск сто тридцять на вісімдесят, скарг на біль у грудях немає.',
     true),
    ('uk-abbrev-001',
     'АТ стабільний, ЧСС у межах норми, ЕКГ без гострої динаміки.',
     'А Те стабільний, Че Ес Ес у межах норми, Е Ка Ге без гострої динаміки.',
     false),
    ('uk-abbrev-002',
     'HbA1c 6,9, глюкоза натще 5,8.',
     'Ейч Бі Ей Один Сі шість і дев''ять, глюкоза натще п''ять і вісім.',
     false),
    ('uk-abbrev-003',
     'КТ органів грудної клітки без контрастування, МРТ не проводилось.',
     'Ка Те органів грудної клітки без контрастування, Ем Ер Те не проводилось.',
     false),
    ('uk-abbrev-004',
     'Загальний аналіз крові та СРБ призначено на завтра.',
     'Загальний аналіз крові та Це Ер Бе призначено на завтра.',
     false)
) AS v(script_id, old_gold, new_gold, equal);

COMMIT;
