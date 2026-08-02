"""Safety filter — completions are linguistic, never clinical.

Table-driven: every numeric clinical value / dosage / ICD-like code in
the completion must appear verbatim in the typed text, else filtered.
"""

from __future__ import annotations

import pytest
from generation_service.domain.safety_filter import check_completion

PREFIX_PLAIN = "Пацієнт скаржиться на біль у"
PREFIX_WITH_BP = "АТ 140/90 мм рт.ст., пацієнт скаржиться на"
PREFIX_WITH_DOSE = "Призначено аспірин 75 мг щоденно, а також"


@pytest.mark.parametrize(
    ("completion", "prefix", "allowed", "reason"),
    [
        # Plain prose passes.
        ("грудній клітці, що посилюється при навантаженні", PREFIX_PLAIN, True, None),
        ("животі та нудоту після їжі", PREFIX_PLAIN, True, None),
        # Invented blood pressure → filtered as blood_pressure.
        ("грудях, АТ 140/90 мм рт.ст.", PREFIX_PLAIN, False, "blood_pressure"),
        # BP echoed from the prefix is fine.
        ("біль у грудях (АТ 140/90 зафіксовано)", PREFIX_WITH_BP, True, None),
        # Invented dosage → filtered as dosage.
        ("призначено метформін 500 мг двічі на день", PREFIX_PLAIN, False, "dosage"),
        # Dosage echoed from the prefix is fine.
        ("продовжено аспірин 75 мг", PREFIX_WITH_DOSE, True, None),
        # Invented ICD-like code → filtered.
        ("що відповідає діагнозу I21.9", PREFIX_PLAIN, False, "icd_code"),
        # Invented date-like fragment → filtered.
        ("з 12.05.2026 отримує терапію", PREFIX_PLAIN, False, "date_like"),
        # Bare invented number → filtered.
        ("температура тіла підвищена до 38", PREFIX_PLAIN, False, "bare_number"),
        # Number present verbatim in prefix passes.
        ("аспірин 75 мг приймає регулярно", PREFIX_WITH_DOSE, True, None),
    ],
)
def test_filter_matrix(completion: str, prefix: str, allowed: bool, reason: str | None):
    verdict = check_completion(completion, text_before_cursor=prefix)
    assert verdict.allowed is allowed
    if not allowed:
        assert verdict.reason == reason
        assert verdict.matched is not None


def test_bp_components_not_double_reported():
    # "140/90" present in prefix: neither the pair nor its components fire.
    verdict = check_completion(
        "контроль АТ 140/90 продовжено", text_before_cursor=PREFIX_WITH_BP
    )
    assert verdict.allowed


def test_empty_completion_allowed():
    assert check_completion("", text_before_cursor=PREFIX_PLAIN).allowed
