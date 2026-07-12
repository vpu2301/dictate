-- Reverse 0026: delete EXACTLY the starter rows (scoped by content,
-- not a blanket source='system' delete) — future corpus drops must
-- survive a down of this migration.

DELETE FROM autocomplete_snippets WHERE source = 'system' AND (trigger, language) IN (
    ('cv', 'en'),
    ('vitals', 'en'),
    ('plan', 'en'),
    ('cv', 'uk'),
    ('vitals', 'uk'),
    ('ecg', 'uk'),
    ('plan', 'uk')
);

DELETE FROM autocomplete_phrases WHERE source = 'system' AND (phrase, language) IN (
    ('shortness of breath on exertion', 'en'),
    ('chest pain radiating to left arm', 'en'),
    ('regular sinus rhythm', 'en'),
    ('blood pressure 120/80 mmHg', 'en'),
    ('heart rate 72 bpm', 'en'),
    ('history of myocardial infarction', 'en'),
    ('continue beta-blocker therapy', 'en'),
    ('no acute distress', 'en'),
    ('alert and oriented x3', 'en'),
    ('follow up in two weeks', 'en'),
    ('задишка при фізичному навантаженні', 'uk'),
    ('біль за грудиною стискаючого характеру', 'uk'),
    ('ритм синусовий правильний', 'uk'),
    ('АТ 120/80 мм рт ст', 'uk'),
    ('ЧСС 72 за хвилину', 'uk'),
    ('тони серця ясні ритмічні', 'uk'),
    ('інфаркт міокарда в анамнезі', 'uk'),
    ('гіпертонічна хвороба II ст', 'uk'),
    ('продовжити прийом бета-блокаторів', 'uk'),
    ('повторна консультація через 2 тижні', 'uk'),
    ('температура тіла нормальна', 'uk'),
    ('шкіра звичайного кольору', 'uk'),
    ('свідомість ясна', 'uk'),
    ('скарг на момент огляду не пред’являє', 'uk'),
    ('загальний стан задовільний', 'uk'),
    ('цукровий діабет 2 типу', 'uk'),
    ('глікемія натще', 'uk'),
    ('HbA1c контроль через 3 місяці', 'uk'),
    ('без вогнищевої патології', 'uk'),
    ('легеневі поля прозорі', 'uk')
);
