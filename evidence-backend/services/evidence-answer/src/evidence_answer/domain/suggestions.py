"""Curated starter questions per specialty (`GET /suggestions`).

Curated, not generated: these are the first thing a clinician sees, and a
model-invented "example question" that turns out to have no corpus answer
teaches exactly the wrong thing about the product on the first interaction.

The list is small and uk/en paired. It grows through clinical review, not
through code — the table below is the artifact reviewers read.
"""

from __future__ import annotations

from dataclasses import dataclass

GENERAL = "general"


@dataclass(frozen=True, slots=True)
class Suggestion:
    specialty: str
    uk: str
    en: str


CURATED: tuple[Suggestion, ...] = (
    Suggestion(
        GENERAL,
        "Емпірична антибіотикотерапія негоспітальної пневмонії у дорослих",
        "Empiric antibiotic therapy for community-acquired pneumonia in adults",
    ),
    Suggestion(
        GENERAL,
        "Коли призначати антикоагулянти при фібриляції передсердь",
        "When to start anticoagulation in atrial fibrillation",
    ),
    Suggestion(
        GENERAL,
        "Корекція дози при хронічній хворобі нирок 4 стадії",
        "Dose adjustment in stage 4 chronic kidney disease",
    ),
    Suggestion(
        "cardiology",
        "Цільовий артеріальний тиск у пацієнтів із цукровим діабетом",
        "Blood pressure targets in patients with diabetes",
    ),
    Suggestion(
        "cardiology",
        "Показання до подвійної антитромбоцитарної терапії після ЧКВ",
        "Indications for dual antiplatelet therapy after PCI",
    ),
    Suggestion(
        "infectious_disease",
        "Тривалість терапії при неускладненій інфекції сечових шляхів",
        "Treatment duration for uncomplicated urinary tract infection",
    ),
    Suggestion(
        "infectious_disease",
        "Профілактика після контакту з менінгококовою інфекцією",
        "Post-exposure prophylaxis after meningococcal contact",
    ),
    Suggestion(
        "pulmonology",
        "Критерії загострення ХОЗЛ і показання до системних кортикостероїдів",
        "COPD exacerbation criteria and indications for systemic corticosteroids",
    ),
    Suggestion(
        "nephrology",
        "Ведення гіперкаліємії при хронічній хворобі нирок",
        "Management of hyperkalemia in chronic kidney disease",
    ),
    Suggestion(
        "endocrinology",
        "Стартова терапія цукрового діабету 2 типу у пацієнта з ССЗ",
        "First-line therapy for type 2 diabetes in a patient with cardiovascular disease",
    ),
)


def curated_for(specialty: str | None, locale: str) -> list[str]:
    """Curated questions for a specialty, general ones always included."""
    wanted = (specialty or "").strip().casefold()
    uk = locale.split("-")[0].casefold() == "uk"
    return [
        (s.uk if uk else s.en)
        for s in CURATED
        if s.specialty == GENERAL or (wanted and s.specialty == wanted)
    ]


def specialties() -> list[str]:
    return sorted({s.specialty for s in CURATED} - {GENERAL})
