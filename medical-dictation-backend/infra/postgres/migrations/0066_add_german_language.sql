-- 0066 — German (`de`) as a third dictation language.
--
-- Scope is the STREAMING DICTATION path and the NLP post-processing it
-- calls: `dictation_sessions` (the session row), `medical_prompts` (the
-- Whisper bias prompt a session must name), `voice_commands` (stage 1 of
-- the NLP pipeline) and `abbreviation_dictionary` (stage 5). Batch ASR
-- (`transcription_jobs`), templates and the eval corpus deliberately
-- stay `('uk','en')` — their edges still reject `de` and widening them
-- without German clinical content would only move the failure later.
--
-- Every CHECK below is a widening, so no existing row can violate it and
-- the migration takes no table rewrite (Postgres still scans to validate;
-- these tables are small).

BEGIN;

-- ── dictation_sessions ──────────────────────────────────────────────

ALTER TABLE dictation_sessions
    DROP CONSTRAINT IF EXISTS dictation_sessions_language_check;
ALTER TABLE dictation_sessions
    ADD CONSTRAINT dictation_sessions_language_check
    CHECK (language IN ('uk','en','de'));

-- ── medical_prompts ─────────────────────────────────────────────────

ALTER TABLE medical_prompts
    DROP CONSTRAINT IF EXISTS medical_prompts_language_check;
ALTER TABLE medical_prompts
    ADD CONSTRAINT medical_prompts_language_check
    CHECK (language IN ('uk','en','de'));

-- German prompts, one default per specialty — mirrors the uk/en set in
-- infra/postgres/seed/medical_prompts.sql (kept identical there so
-- `make seed-prompts` converges to the same rows). Each ≤ 224 tokens.
INSERT INTO medical_prompts (language, specialty, prompt_text, version, is_default) VALUES
  ('de', 'cardiology',
   'Kardiologische Konsultation. Thoraxschmerz, Dyspnoe, Palpitationen, Synkope. Kardiovaskuläre Risikofaktoren, Blutdruck, Herzfrequenz, Herzauskultation, EKG, Echokardiographie. Troponin, NT-proBNP. NYHA-Klassifikation. Betablocker, ACE-Hemmer, Statine, Thrombozytenaggregationshemmer.',
   1, true),
  ('de', 'endocrinology',
   'Endokrinologische Vorstellung. Diabetes mellitus, Schilddrüsenerkrankung, metabolisches Syndrom, Adipositas. HbA1c, Nüchternglukose, TSH, freies T4, TPO-Antikörper. Metformin, Insulin, Levothyroxin. Dosisanpassung.',
   1, true),
  ('de', 'gastroenterology',
   'Gastroenterologische Konsultation. Bauchschmerzen, Dyspepsie, Übelkeit, Erbrechen, Sodbrennen, veränderter Stuhlgang. Gastroskopie, Koloskopie, Abdomensonographie, Leberwerte, Lipase, Amylase. Diagnosen: Gastritis, Ulkuskrankheit, Refluxkrankheit, Reizdarmsyndrom, Hepatitis.',
   1, true),
  ('de', 'neurology',
   'Neurologische Untersuchung. Kopfschmerz, Schwindel, Parästhesien, Paresen, Krampfanfälle, Ataxie. Hirnnerven, Muskelkraft, Reflexe, Sensibilität. MRT des Schädels, EEG, EMG. Diagnosen: Migräne, Schlaganfall, Epilepsie, Polyneuropathie.',
   1, true),
  ('de', 'orthopedics',
   'Orthopädische Vorstellung. Gelenkschmerzen, Bewegungseinschränkung, Trauma, Arthrose, Osteoporose, Bandscheibenvorfall. Röntgen, MRT, CT, DXA-Osteodensitometrie. NSAR, Chondroprotektiva, Physiotherapie, operative Versorgung.',
   1, true),
  ('de', 'pediatrics',
   'Pädiatrische Untersuchung. Impfstatus, Wachstum und Entwicklungsmeilensteine, Anthropometrie, Körpertemperatur. Akute Atemwegsinfekte, Bronchitis, Pneumonie, Gastroenteritis. Dosierung nach Körpergewicht und Alter.',
   1, true),
  ('de', 'general',
   'Allgemeinmedizinische Konsultation. Aktuelle Beschwerden, Vorerkrankungen, Anamnese, körperliche Untersuchung, Vitalparameter, Blutdruck, Herzfrequenz, Sauerstoffsättigung. Blutbild, Basislabor, Urinstatus. Symptomatische und kausale Therapie.',
   1, true)
ON CONFLICT DO NOTHING;

-- ── voice_commands ──────────────────────────────────────────────────

ALTER TABLE voice_commands
    DROP CONSTRAINT IF EXISTS voice_commands_language_check;
ALTER TABLE voice_commands
    ADD CONSTRAINT voice_commands_language_check
    CHECK (language IN ('uk','en','de'));

-- The rows themselves come from
-- infra/postgres/seed/voice_commands_de.json via
-- `make seed-voice-commands` (see the seeding note in 0055): the JSON
-- fixtures are authoritative and the seeder DELETEs a language before
-- re-inserting it. Nothing is inserted here.

-- ── abbreviation_dictionary ─────────────────────────────────────────

ALTER TABLE abbreviation_dictionary
    DROP CONSTRAINT IF EXISTS abbreviation_dictionary_language_check;
ALTER TABLE abbreviation_dictionary
    ADD CONSTRAINT abbreviation_dictionary_language_check
    CHECK (language IN ('uk','en','de'));

-- Global German rules (tenant_id IS NULL), mirroring
-- infra/postgres/seed/abbreviations_global.sql. `compact` = the spoken
-- long form is written as the abbreviation.
INSERT INTO abbreviation_dictionary
    (tenant_id, language, expanded, abbreviated, direction, domain, case_sensitive)
VALUES
  (NULL, 'de', 'Blutdruck', 'RR', 'compact', 'all', true),
  (NULL, 'de', 'Herzfrequenz', 'HF', 'compact', 'all', true),
  (NULL, 'de', 'Atemfrequenz', 'AF', 'compact', 'all', true),
  (NULL, 'de', 'Elektrokardiogramm', 'EKG', 'compact', 'all', true),
  (NULL, 'de', 'Magnetresonanztomographie', 'MRT', 'compact', 'all', true),
  (NULL, 'de', 'Computertomographie', 'CT', 'compact', 'all', true),
  (NULL, 'de', 'Sonographie', 'US', 'compact', 'all', true),
  (NULL, 'de', 'großes Blutbild', 'gr. BB', 'compact', 'all', true),
  (NULL, 'de', 'Myokardinfarkt', 'MI', 'compact', 'cardiology', true),
  (NULL, 'de', 'chronische Herzinsuffizienz', 'CHI', 'compact', 'cardiology', true),
  (NULL, 'de', 'koronare Herzkrankheit', 'KHK', 'compact', 'cardiology', true),
  (NULL, 'de', 'Vorhofflimmern', 'VHF', 'compact', 'cardiology', true),
  (NULL, 'de', 'Diabetes mellitus', 'DM', 'compact', 'endocrinology', true),
  (NULL, 'de', 'arterielle Hypertonie', 'aHT', 'compact', 'cardiology', true),
  (NULL, 'de', 'chronisch obstruktive Lungenerkrankung', 'COPD', 'compact', 'pulmonology', true),
  (NULL, 'de', 'Lungenembolie', 'LE', 'compact', 'cardiology', true),
  (NULL, 'de', 'Echokardiographie', 'Echo', 'compact', 'cardiology', true)
ON CONFLICT (tenant_id, language, expanded, abbreviated) DO NOTHING;

COMMIT;
