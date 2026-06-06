-- Sprint 10 / Day 2 — seed system phrases + snippets.
--
-- Sprint-10 ships a starter corpus (~50 phrases per language across
-- a handful of common specialties) as the structural anchor; the
-- clinical-content-lead deliverable is to expand to ~10k phrases per
-- the spec §2.3 corpus shape.
--
-- The migration is idempotent: ON CONFLICT DO NOTHING.

INSERT INTO autocomplete_phrases (tenant_id, owner_user_id, phrase, language, specialty, section_hint, source)
VALUES
    -- ── UK / cardiology ────────────────────────────────────────────
    (NULL, NULL, 'задишка при фізичному навантаженні', 'uk', 'cardiology', 'anamnesis', 'system'),
    (NULL, NULL, 'біль за грудиною стискаючого характеру', 'uk', 'cardiology', 'anamnesis', 'system'),
    (NULL, NULL, 'ритм синусовий правильний', 'uk', 'cardiology', 'examination', 'system'),
    (NULL, NULL, 'АТ 120/80 мм рт ст', 'uk', 'cardiology', 'examination', 'system'),
    (NULL, NULL, 'ЧСС 72 за хвилину', 'uk', 'cardiology', 'examination', 'system'),
    (NULL, NULL, 'тони серця ясні ритмічні', 'uk', 'cardiology', 'examination', 'system'),
    (NULL, NULL, 'інфаркт міокарда в анамнезі', 'uk', 'cardiology', 'anamnesis', 'system'),
    (NULL, NULL, 'гіпертонічна хвороба II ст', 'uk', 'cardiology', 'diagnosis', 'system'),
    (NULL, NULL, 'продовжити прийом бета-блокаторів', 'uk', 'cardiology', 'plan', 'system'),
    (NULL, NULL, 'повторна консультація через 2 тижні', 'uk', 'cardiology', 'follow_up', 'system'),
    -- ── UK / general ───────────────────────────────────────────────
    (NULL, NULL, 'температура тіла нормальна', 'uk', 'general', 'examination', 'system'),
    (NULL, NULL, 'шкіра звичайного кольору', 'uk', 'general', 'examination', 'system'),
    (NULL, NULL, 'свідомість ясна', 'uk', 'general', 'examination', 'system'),
    (NULL, NULL, 'скарг на момент огляду не пред''являє', 'uk', 'general', 'anamnesis', 'system'),
    (NULL, NULL, 'загальний стан задовільний', 'uk', 'general', 'examination', 'system'),
    -- ── UK / endocrinology ─────────────────────────────────────────
    (NULL, NULL, 'цукровий діабет 2 типу', 'uk', 'endocrinology', 'diagnosis', 'system'),
    (NULL, NULL, 'глікемія натще', 'uk', 'endocrinology', 'examination', 'system'),
    (NULL, NULL, 'HbA1c контроль через 3 місяці', 'uk', 'endocrinology', 'follow_up', 'system'),
    -- ── UK / radiology ─────────────────────────────────────────────
    (NULL, NULL, 'без вогнищевої патології', 'uk', 'radiology', 'examination', 'system'),
    (NULL, NULL, 'легеневі поля прозорі', 'uk', 'radiology', 'examination', 'system'),
    -- ── EN / cardiology ────────────────────────────────────────────
    (NULL, NULL, 'shortness of breath on exertion', 'en', 'cardiology', 'anamnesis', 'system'),
    (NULL, NULL, 'chest pain radiating to left arm', 'en', 'cardiology', 'anamnesis', 'system'),
    (NULL, NULL, 'regular sinus rhythm', 'en', 'cardiology', 'examination', 'system'),
    (NULL, NULL, 'blood pressure 120/80 mmHg', 'en', 'cardiology', 'examination', 'system'),
    (NULL, NULL, 'heart rate 72 bpm', 'en', 'cardiology', 'examination', 'system'),
    (NULL, NULL, 'history of myocardial infarction', 'en', 'cardiology', 'anamnesis', 'system'),
    (NULL, NULL, 'continue beta-blocker therapy', 'en', 'cardiology', 'plan', 'system'),
    -- ── EN / general ───────────────────────────────────────────────
    (NULL, NULL, 'no acute distress', 'en', 'general', 'examination', 'system'),
    (NULL, NULL, 'alert and oriented x3', 'en', 'general', 'examination', 'system'),
    (NULL, NULL, 'follow up in two weeks', 'en', 'general', 'follow_up', 'system')
ON CONFLICT DO NOTHING;

-- ── System snippets ────────────────────────────────────────────────

INSERT INTO autocomplete_snippets (tenant_id, owner_user_id, trigger, expansion, cursor_position, language, source)
VALUES
    (NULL, NULL, 'cv', 'Серцево-судинна система: ритм синусовий правильний, тони ясні ритмічні, АТ {_} мм рт ст, ЧСС {_} за хвилину, шумів не вислуховується.', 70, 'uk', 'system'),
    (NULL, NULL, 'vitals', 'Температура {_} °C, АТ {_} мм рт ст, ЧСС {_} за хвилину, ЧДР {_} за хвилину, SpO₂ {_}%.', 13, 'uk', 'system'),
    (NULL, NULL, 'ecg', 'ЕКГ: ритм синусовий, ЧСС {_} за хвилину, без особливостей.', 21, 'uk', 'system'),
    (NULL, NULL, 'plan', 'План: {_}', 7, 'uk', 'system'),
    (NULL, NULL, 'cv', 'Cardiovascular: regular sinus rhythm, no murmurs, BP {_} mmHg, HR {_} bpm.', 50, 'en', 'system'),
    (NULL, NULL, 'vitals', 'Vitals: T {_} °C, BP {_} mmHg, HR {_} bpm, RR {_} /min, SpO₂ {_}%.', 11, 'en', 'system'),
    (NULL, NULL, 'plan', 'Plan: {_}', 6, 'en', 'system')
ON CONFLICT DO NOTHING;
