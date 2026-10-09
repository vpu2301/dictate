"""The WER eval recording script, vendored server-side.

The console recorder (SPA /company/corpus/record) may only record lines that
appear here — that is the privacy invariant the whole recorder is built
around: no free-form capture, so no real patient can end up in the eval
corpus. The SPA fetches this script over GET /corpus/eval/script and the
take-upload route refuses any script_id (or drifted text) it does not find
here, which keeps the invariant server-enforced rather than trusted to the
browser.

Content mirrors the SPA's starter set (src/corpus/evalScript.js, sprint 21):
a working script covering every adversarial subset from
eval/corpus/v1/subsets/README.md, with the sprint-18 dependency
(phone_mic_noisy) deliberately over-represented. Growing the corpus means
editing SCRIPT here — the SPA renders whatever this serves.

Every line is synthetic. Two texts per row: `say` is the spoken form read
aloud, and `transcript` is the gold — which, since corpus-v3 Epic B, is the
SAME TEXT for every line here, so no row overrides it any more.

WHY THE GOLD MOVED. Until v3 the gold was the post-NLP written form: "АТ
140/90 мм рт. ст." against a spoken "сто сорок на дев'яносто". That made
raw WER a measurement of two things at once — whether the model heard the
words, and whether the writing convention matched — and the v2 numbers
could not separate them (18.1% raw against 14.4% normalised, most of the
gap being style). Epic B settles it: the reference is what was said, the
normaliser is the safety net for the written variants, and eval_goldlint
keeps new lines honest. Migration 0093 journals the 14 lines this changed,
so any run scored before it is marked "еталон змінено" rather than quietly
compared against a corpus that has moved.

The `transcript` key still exists in the row schema and in the authored
half of the script: a line MAY carry a gold that differs from its spoken
form, and the linter will warn about it rather than refuse it. Nothing
vendored uses that any more.
"""

from __future__ import annotations

from typing import Any, Final

#: Bumped by Epic B. Takes record the script version they were read from, so
#: audio captured against the old golds stays identifiable as such.
SCRIPT_VERSION: Final = "v2"

DICTATION_SOURCE: Final = "authored_by_clinician"

SUBSETS: Final = (
    "numbers_doses_units",
    "drug_names",
    "abbreviations",
    "code_switching",
    "voice_commands",
    "phone_mic_noisy",
)

CONDITIONS: Final = ("headset", "laptop-mic", "phone-speaker-distance", "noisy")

# One dict per utterance: id, subset (None = baseline), language, specialty,
# say, and optionally transcript (gold, when it differs) and condition (a
# suggested recording condition the SPA pre-selects).
SCRIPT: Final[tuple[dict[str, Any], ...]] = (
    # ── baseline: clean dictation, one per specialty ──────────────────────
    {"id": "uk-cardiology-101", "subset": None, "language": "uk", "specialty": "cardiology",
     "say": "Скарги на задишку при помірному навантаженні та серцебиття протягом трьох тижнів."},
    {"id": "uk-radiology-101", "subset": None, "language": "uk", "specialty": "radiology",
     "say": "Легеневі поля прозорі, вогнищевих та інфільтративних змін не виявлено."},
    {"id": "uk-endocrinology-101", "subset": None, "language": "uk", "specialty": "endocrinology",
     "say": "Щитоподібна залоза не збільшена, при пальпації безболісна, вузлів не визначається."},
    {"id": "uk-general-101", "subset": None, "language": "uk", "specialty": "general",
     "say": "Загальний стан задовільний, шкірні покриви звичайного кольору, набряків немає."},
    {"id": "en-cardiology-101", "subset": None, "language": "en", "specialty": "cardiology",
     "say": "The patient reports exertional dyspnoea and palpitations over the past three weeks."},
    {"id": "en-radiology-101", "subset": None, "language": "en", "specialty": "radiology",
     "say": "Lung fields are clear with no focal consolidation or pleural effusion."},

    # ── numbers, doses, units ─────────────────────────────────────────────
    {"id": "uk-numbers-001", "subset": "numbers_doses_units", "language": "uk", "specialty": "cardiology",
     "say": "Артеріальний тиск сто сорок на дев'яносто міліметрів ртутного стовпа, пульс сімдесят два за хвилину."},
    {"id": "uk-numbers-002", "subset": "numbers_doses_units", "language": "uk", "specialty": "endocrinology",
     "say": "Глікований гемоглобін сім цілих дві десятих відсотка."},
    {"id": "uk-numbers-003", "subset": "numbers_doses_units", "language": "uk", "specialty": "internal_medicine",
     "say": "Призначено двадцять п'ять міліграмів двічі на добу протягом чотирнадцяти днів."},
    {"id": "uk-numbers-004", "subset": "numbers_doses_units", "language": "uk", "specialty": "general",
     "say": "Температура тіла тридцять сім і вісім, частота дихання вісімнадцять за хвилину."},
    {"id": "uk-numbers-005", "subset": "numbers_doses_units", "language": "uk", "specialty": "radiology",
     "say": "Вузол діаметром дванадцять на вісім міліметрів у нижній частці справа."},
    {"id": "en-numbers-001", "subset": "numbers_doses_units", "language": "en", "specialty": "cardiology",
     "say": "Blood pressure one thirty eight over eighty four, heart rate sixty six beats per minute."},

    # ── drug names ────────────────────────────────────────────────────────
    {"id": "uk-drugs-001", "subset": "drug_names", "language": "uk", "specialty": "cardiology",
     "say": "Продовжити бісопролол п'ять міліграмів вранці та розувастатин десять міліграмів увечері."},
    {"id": "uk-drugs-002", "subset": "drug_names", "language": "uk", "specialty": "endocrinology",
     "say": "Метформін тисяча міліграмів двічі на добу після їжі."},
    {"id": "uk-drugs-003", "subset": "drug_names", "language": "uk", "specialty": "internal_medicine",
     "say": "Пантопразол сорок міліграмів натще, амоксицилін клавуланова кислота вісімсот сімдесят п'ять на сто двадцять п'ять."},
    {"id": "uk-drugs-004", "subset": "drug_names", "language": "uk", "specialty": "general",
     "say": "Алергія на ібупрофен в анамнезі, парацетамол переносить задовільно."},

    # ── abbreviations ─────────────────────────────────────────────────────
    {"id": "uk-abbrev-001", "subset": "abbreviations", "language": "uk", "specialty": "cardiology",
     "say": "А Те стабільний, Че Ес Ес у межах норми, Е Ка Ге без гострої динаміки."},
    {"id": "uk-abbrev-002", "subset": "abbreviations", "language": "uk", "specialty": "endocrinology",
     "say": "Ейч Бі Ей Один Сі шість і дев'ять, глюкоза натще п'ять і вісім."},
    {"id": "uk-abbrev-003", "subset": "abbreviations", "language": "uk", "specialty": "radiology",
     "say": "Ка Те органів грудної клітки без контрастування, Ем Ер Те не проводилось."},
    {"id": "uk-abbrev-004", "subset": "abbreviations", "language": "uk", "specialty": "internal_medicine",
     "say": "Загальний аналіз крові та Це Ер Бе призначено на завтра."},

    # ── code switching ────────────────────────────────────────────────────
    {"id": "uk-codesw-001", "subset": "code_switching", "language": "uk", "specialty": "cardiology",
     "say": "За даними ехокардіографії ejection fraction збережена, регіонарних порушень немає."},
    {"id": "uk-codesw-002", "subset": "code_switching", "language": "uk", "specialty": "radiology",
     "say": "У проекції ground glass opacity в нижній частці лівої легені."},
    {"id": "uk-codesw-003", "subset": "code_switching", "language": "uk", "specialty": "internal_medicine",
     "say": "Діагноз formulated as diabetes mellitus type two, компенсація задовільна."},
    {"id": "uk-codesw-004", "subset": "code_switching", "language": "uk", "specialty": "general",
     "say": "Пацієнту рекомендовано follow-up через три місяці у сімейного лікаря."},

    # ── voice commands ────────────────────────────────────────────────────
    {"id": "uk-cmd-001", "subset": "voice_commands", "language": "uk", "specialty": "general",
     "say": "Новий абзац. Об'єктивно: стан задовільний, свідомість ясна."},
    {"id": "uk-cmd-002", "subset": "voice_commands", "language": "uk", "specialty": "cardiology",
     "say": "Скасувати останнє речення. Тони серця приглушені, ритм правильний."},
    {"id": "uk-cmd-003", "subset": "voice_commands", "language": "uk", "specialty": "general",
     "say": "Пацієнт просив новий рецепт і новий абзац у виписці — це не команда, а цитата."},
    {"id": "uk-cmd-004", "subset": "voice_commands", "language": "uk", "specialty": "radiology",
     "say": "Перейти до висновку. Ознак вогнищевої патології не виявлено."},

    # ── phone / distance / noise — the sprint-18 dependency ───────────────
    {"id": "uk-noisy-001", "subset": "phone_mic_noisy", "language": "uk", "specialty": "general",
     "condition": "phone-speaker-distance",
     "say": "Об'єктивно: дихання везикулярне, хрипів немає, живіт м'який, безболісний."},
    {"id": "uk-noisy-002", "subset": "phone_mic_noisy", "language": "uk", "specialty": "cardiology",
     "condition": "phone-speaker-distance",
     "say": "Артеріальний тиск сто тридцять на вісімдесят, скарг на біль у грудях немає."},
    {"id": "uk-noisy-003", "subset": "phone_mic_noisy", "language": "uk", "specialty": "general",
     "condition": "noisy",
     "say": "Рекомендовано контрольний огляд через два тижні та повторний аналіз крові."},
    {"id": "uk-noisy-004", "subset": "phone_mic_noisy", "language": "uk", "specialty": "internal_medicine",
     "condition": "noisy",
     "say": "Скарги на загальну слабкість, зниження апетиту та порушення сну протягом місяця."},
    {"id": "uk-noisy-005", "subset": "phone_mic_noisy", "language": "uk", "specialty": "endocrinology",
     "condition": "phone-speaker-distance",
     "say": "Продовжити терапію в попередньому обсязі, наступний візит через три місяці."},
    {"id": "en-noisy-001", "subset": "phone_mic_noisy", "language": "en", "specialty": "general",
     "condition": "noisy",
     "say": "The patient remains stable with no new complaints since the last review."},
)

ROW_BY_ID: Final[dict[str, dict[str, Any]]] = {row["id"]: row for row in SCRIPT}


def gold_transcript(row: dict[str, Any]) -> str:
    """The gold text for a row — `transcript` when the spoken form differs."""
    return str(row.get("transcript") or row["say"])


def target_path(row: dict[str, Any]) -> str:
    """Where this row's files belong under eval/corpus/v1/."""
    subset = row.get("subset")
    return f"subsets/{subset}/{row['id']}" if subset else str(row["id"])
