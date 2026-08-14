"""Risk-flag lexicon (over-flag bias) + exhaustive tier routing table."""

import pytest
from corpus_forge.domain.risk import RiskFlagger
from corpus_forge.domain.tiers import SOURCE_KINDS, route_tier

FLAGGER = RiskFlagger(
    drug_lexicon=frozenset({"бісопролол", "bisoprolol"}),
    abbrev_allowlist=frozenset({"ЕКГ", "ECG"}),
)


class TestRiskFlags:
    def test_digits_flag_dose(self) -> None:
        assert "dose" in FLAGGER.flags("прийом 5 мг на добу")

    def test_dose_units_without_digits_flag_dose(self) -> None:
        assert "dose" in FLAGGER.flags("п’ять міліграм мг")

    def test_drug_lexicon(self) -> None:
        assert "drug" in FLAGGER.flags("призначено бісопролол щоденно")

    def test_laterality_uk_and_en(self) -> None:
        assert "laterality" in FLAGGER.flags("притуплення зліва в нижніх відділах")
        assert "laterality" in FLAGGER.flags("лівий шлуночок не розширений")
        assert "laterality" in FLAGGER.flags("right lower lobe consolidation")

    def test_negation(self) -> None:
        assert "negation" in FLAGGER.flags("хрипів немає")
        assert "negation" in FLAGGER.flags("без патологічних змін")
        assert "negation" in FLAGGER.flags("no acute distress")

    def test_icd_code(self) -> None:
        assert "icd" in FLAGGER.flags("діагноз I11.9 гіпертензивна хвороба серця")

    def test_unfamiliar_abbrev_flagged_allowlisted_not(self) -> None:
        assert "abbrev" in FLAGGER.flags("АТ в межах норми")  # not allowlisted → tier 3
        assert "abbrev" not in FLAGGER.flags("ЕКГ без динаміки")

    def test_clean_phrase_no_flags(self) -> None:
        assert FLAGGER.flags("загальний стан задовільний") == []


class TestTierRouting:
    """Exhaustive: every (source_kind, flags?, validators?) combination."""

    @pytest.mark.parametrize("kind", SOURCE_KINDS)
    @pytest.mark.parametrize("validators_passed", [True, False])
    def test_any_risk_flag_is_tier_3_no_exceptions(self, kind: str, validators_passed: bool) -> None:
        assert route_tier(source_kind=kind, risk_flags=["dose"], validators_passed=validators_passed) == 3

    def test_mined_validated_clean_is_tier_1(self) -> None:
        assert route_tier(source_kind="mined", risk_flags=[], validators_passed=True) == 1

    def test_mined_unvalidated_is_tier_2(self) -> None:
        assert route_tier(source_kind="mined", risk_flags=[], validators_passed=False) == 2

    @pytest.mark.parametrize("kind", ["telemetry_gap", "terminology", "generated", "authored"])
    @pytest.mark.parametrize("validators_passed", [True, False])
    def test_everything_else_clean_is_tier_2(self, kind: str, validators_passed: bool) -> None:
        assert route_tier(source_kind=kind, risk_flags=[], validators_passed=validators_passed) == 2

    def test_unknown_source_kind_raises(self) -> None:
        with pytest.raises(ValueError):
            route_tier(source_kind="scraped", risk_flags=[], validators_passed=True)
