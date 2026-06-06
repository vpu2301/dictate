-- Sprint 05 — global seed for `abbreviation_dictionary`.
--
-- ~40 starter entries reviewed by clinical content lead. Tenants may
-- override via the admin API (PUT /nlp/abbreviations). Direction
-- defaults to ``compact`` (write the abbreviation) — clinics that
-- prefer expansion can override per term.
--
-- Idempotent via ON CONFLICT.

BEGIN;

INSERT INTO abbreviation_dictionary
    (tenant_id, language, expanded, abbreviated, direction, domain, case_sensitive)
VALUES
  -- ── Ukrainian ────────────────────────────────────────────────
  (NULL, 'uk', 'артеріальний тиск', 'АТ', 'compact', 'all', true),
  (NULL, 'uk', 'частота серцевих скорочень', 'ЧСС', 'compact', 'all', true),
  (NULL, 'uk', 'частота дихання', 'ЧД', 'compact', 'all', true),
  (NULL, 'uk', 'електрокардіографія', 'ЕКГ', 'compact', 'all', true),
  (NULL, 'uk', 'магнітно-резонансна томографія', 'МРТ', 'compact', 'all', true),
  (NULL, 'uk', 'компʼютерна томографія', 'КТ', 'compact', 'all', true),
  (NULL, 'uk', 'ультразвукове дослідження', 'УЗД', 'compact', 'all', true),
  (NULL, 'uk', 'загальний аналіз крові', 'ЗАК', 'compact', 'all', true),
  (NULL, 'uk', 'біохімічний аналіз крові', 'БАК', 'compact', 'all', true),
  (NULL, 'uk', 'інфаркт міокарда', 'ІМ', 'compact', 'cardiology', true),
  (NULL, 'uk', 'хронічна серцева недостатність', 'ХСН', 'compact', 'cardiology', true),
  (NULL, 'uk', 'ішемічна хвороба серця', 'ІХС', 'compact', 'cardiology', true),
  (NULL, 'uk', 'фібриляція передсердь', 'ФП', 'compact', 'cardiology', true),
  (NULL, 'uk', 'цукровий діабет', 'ЦД', 'compact', 'endocrinology', true),
  (NULL, 'uk', 'гіпертонічна хвороба', 'ГХ', 'compact', 'cardiology', true),
  (NULL, 'uk', 'інсулінозалежний цукровий діабет', 'ІЗЦД', 'compact', 'endocrinology', true),
  (NULL, 'uk', 'хронічна обструктивна хвороба легень', 'ХОЗЛ', 'compact', 'pulmonology', true),
  (NULL, 'uk', 'тромбоемболія легеневої артерії', 'ТЕЛА', 'compact', 'cardiology', true),
  (NULL, 'uk', 'ехокардіографія', 'ЕхоКГ', 'compact', 'cardiology', true),
  (NULL, 'uk', 'центральна нервова система', 'ЦНС', 'compact', 'neurology', true),
  -- ── English ──────────────────────────────────────────────────
  (NULL, 'en', 'blood pressure', 'BP', 'compact', 'all', true),
  (NULL, 'en', 'heart rate', 'HR', 'compact', 'all', true),
  (NULL, 'en', 'respiratory rate', 'RR', 'compact', 'all', true),
  (NULL, 'en', 'electrocardiogram', 'ECG', 'compact', 'all', true),
  (NULL, 'en', 'magnetic resonance imaging', 'MRI', 'compact', 'all', true),
  (NULL, 'en', 'computed tomography', 'CT', 'compact', 'all', true),
  (NULL, 'en', 'ultrasound', 'US', 'compact', 'all', true),
  (NULL, 'en', 'complete blood count', 'CBC', 'compact', 'all', true),
  (NULL, 'en', 'basic metabolic panel', 'BMP', 'compact', 'all', true),
  (NULL, 'en', 'myocardial infarction', 'MI', 'compact', 'cardiology', true),
  (NULL, 'en', 'congestive heart failure', 'CHF', 'compact', 'cardiology', true),
  (NULL, 'en', 'coronary artery disease', 'CAD', 'compact', 'cardiology', true),
  (NULL, 'en', 'atrial fibrillation', 'AFib', 'compact', 'cardiology', true),
  (NULL, 'en', 'diabetes mellitus', 'DM', 'compact', 'endocrinology', true),
  (NULL, 'en', 'hypertension', 'HTN', 'compact', 'cardiology', true),
  (NULL, 'en', 'type 2 diabetes mellitus', 'T2DM', 'compact', 'endocrinology', true),
  (NULL, 'en', 'chronic obstructive pulmonary disease', 'COPD', 'compact', 'pulmonology', true),
  (NULL, 'en', 'pulmonary embolism', 'PE', 'compact', 'cardiology', true),
  (NULL, 'en', 'echocardiography', 'echo', 'compact', 'cardiology', false),
  (NULL, 'en', 'central nervous system', 'CNS', 'compact', 'neurology', true)
ON CONFLICT DO NOTHING;

COMMIT;
