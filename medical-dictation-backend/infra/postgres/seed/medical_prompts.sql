-- Sprint 03 — seed `medical_prompts` with 14 Ukrainian / English clinical
-- prompts (2 languages × 7 specialties). Hand-authored by the clinical
-- content lead; each ≤ 224 tokens to fit Whisper's prompt context.
--
-- Idempotent: ON CONFLICT DO NOTHING on (language, specialty, is_default).
-- Run via `make seed-prompts` or include from migration 0008's seed step
-- in CI's `make ci-with-db`.

BEGIN;

INSERT INTO medical_prompts (language, specialty, prompt_text, version, is_default) VALUES
  -- ── Ukrainian ──────────────────────────────────────────────────────
  ('uk', 'cardiology',
   'Кардіологічна консультація. Скарги, анамнез захворювання, фактори ризику, артеріальний тиск, ЧСС, аускультація серця, ЕКГ, ехокардіографія, тропоніни, NT-proBNP. Діагноз за класифікацією NYHA. Призначення: бета-блокатори, інгібітори АПФ, статини, антиагреганти.',
   1, true),
  ('uk', 'endocrinology',
   'Ендокринологічний огляд. Цукровий діабет, тиреоїдна патологія, метаболічний синдром, ожиріння. HbA1c, глюкоза натще, ТТГ, вільний Т4, антитіла до ТПО. Терапія: метформін, інсулін, левотироксин. Корекція дози.',
   1, true),
  ('uk', 'gastroenterology',
   'Гастроентерологічна консультація. Біль у животі, диспепсія, нудота, блювання, печія, зміна випорожнень. ФГДС, колоноскопія, УЗД органів черевної порожнини, печінкові проби, ліпаза, амілаза. Діагнози: гастрит, виразкова хвороба, ГЕРХ, синдром подразненого кишечника, гепатит.',
   1, true),
  ('uk', 'neurology',
   'Неврологічний огляд. Головний біль, запаморочення, парестезії, парези, судоми, порушення координації. Дослідження черепних нервів, м''язевої сили, рефлексів, чутливості. МРТ головного мозку, ЕЕГ, ЕНМГ. Діагнози: мігрень, інсульт, епілепсія, поліневропатія.',
   1, true),
  ('uk', 'orthopedics',
   'Ортопедичний огляд. Біль у суглобах, обмеження рухів, травми, остеоартроз, остеопороз, грижі міжхребцевих дисків. Рентгенографія, МРТ, КТ, денситометрія. Лікування: НПЗП, хондропротектори, фізіотерапія, оперативне лікування.',
   1, true),
  ('uk', 'pediatrics',
   'Педіатричний огляд. Вакцинальний статус, фізичний та психомоторний розвиток, антропометрія, температура тіла. Гострі респіраторні інфекції, бронхіт, пневмонія, кишкові інфекції. Призначення з урахуванням маси тіла та віку.',
   1, true),
  ('uk', 'general',
   'Загальний терапевтичний прийом. Скарги, анамнез життя та захворювання, об''єктивний огляд, артеріальний тиск, пульс, сатурація. Загальний аналіз крові, біохімія, аналіз сечі. Призначення симптоматичної та етіотропної терапії.',
   1, true),

  -- ── English ────────────────────────────────────────────────────────
  ('en', 'cardiology',
   'Cardiology consultation. Chest pain, dyspnea, palpitations, syncope. Cardiovascular risk factors, blood pressure, heart rate, cardiac auscultation, ECG, echocardiography. Troponin, NT-proBNP. NYHA classification. Beta-blockers, ACE inhibitors, statins, antiplatelets.',
   1, true),
  ('en', 'endocrinology',
   'Endocrinology visit. Diabetes mellitus, thyroid disease, metabolic syndrome, obesity. HbA1c, fasting glucose, TSH, free T4, TPO antibodies. Metformin, insulin, levothyroxine. Dose adjustment.',
   1, true),
  ('en', 'gastroenterology',
   'Gastroenterology consultation. Abdominal pain, dyspepsia, nausea, vomiting, heartburn, altered bowel habits. EGD, colonoscopy, abdominal ultrasound, liver function tests, lipase, amylase. Diagnoses: gastritis, peptic ulcer, GERD, irritable bowel syndrome, hepatitis.',
   1, true),
  ('en', 'neurology',
   'Neurology examination. Headache, dizziness, paresthesia, paresis, seizures, ataxia. Cranial nerves, motor strength, reflexes, sensation. MRI brain, EEG, EMG. Diagnoses: migraine, stroke, epilepsy, polyneuropathy.',
   1, true),
  ('en', 'orthopedics',
   'Orthopedic visit. Joint pain, restricted range of motion, trauma, osteoarthritis, osteoporosis, intervertebral disc herniation. X-ray, MRI, CT, DEXA densitometry. NSAIDs, chondroprotectors, physical therapy, surgical intervention.',
   1, true),
  ('en', 'pediatrics',
   'Pediatric examination. Vaccination status, growth and developmental milestones, anthropometry, body temperature. Acute respiratory infections, bronchitis, pneumonia, gastrointestinal infections. Weight- and age-adjusted dosing.',
   1, true),
  ('en', 'general',
   'General internal medicine encounter. Chief complaint, past medical history, history of present illness, physical examination, vital signs, blood pressure, heart rate, oxygen saturation. Complete blood count, basic metabolic panel, urinalysis. Symptomatic and etiologic treatment.',
   1, true)
ON CONFLICT DO NOTHING;

COMMIT;
