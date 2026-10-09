"""Quota matrix pass/fail + validator v2 release checks."""

from pathlib import Path

from corpus_forge.domain.quota import QuotaCell, QuotaConfig, check_quota, length_bucket, load_quota
from corpus_forge.domain.release import ReleaseRow, render_release_csv
from corpus_forge.domain.validate import validate_release_csv

CONFIG = QuotaConfig(
    lower=0.70,
    upper=1.30,
    cells=(QuotaCell(language="uk", specialty="cardiology", section="diagnosis", bucket="short", target=10),),
)


def _rows(n: int) -> list[tuple[str, str, str | None, str | None]]:
    return [(f"діагноз номер {i}", "uk", "cardiology", "diagnosis") for i in range(n)]


class TestQuota:
    def test_bucket_boundary(self) -> None:
        assert length_bucket("один два три чотири п’ять") == "short"
        assert length_bucket("один два три чотири п’ять шість") == "long"

    def test_within_tolerance_passes(self) -> None:
        assert check_quota(CONFIG, _rows(7)) == []  # 70% of 10
        assert check_quota(CONFIG, _rows(13)) == []  # 130% of 10

    def test_under_and_over_fail(self) -> None:
        assert check_quota(CONFIG, _rows(6))[0].bound == "under"
        assert check_quota(CONFIG, _rows(14))[0].bound == "over"

    def test_shipped_quota_yaml_loads(self) -> None:
        root = Path(__file__).resolve().parents[4] / "infra/seeds/corpus/quota.yaml"
        config = load_quota(root)
        assert config.lower == 0.70 and config.upper == 1.30
        assert len(config.cells) > 20


def _release_csv(rows: list[ReleaseRow]) -> str:
    return render_release_csv(rows)


def _row(**overrides: object) -> ReleaseRow:
    base: dict[str, object] = {
        "phrase": "загальний стан хворого задовільний",
        "language": "uk",
        "specialty": "general",
        "section_hint": "examination",
        "source_kind": "terminology",
        "source_ref": "drlz:2026-08-09:abc",
        "tier": 2,
        "review_engine": "human",
        "risk_flags": (),
    }
    base.update(overrides)
    return ReleaseRow(**base)  # type: ignore[arg-type]


class TestValidateRelease:
    def test_clean_release_passes(self) -> None:
        assert validate_release_csv(_release_csv([_row()])) == []

    def test_risk_flags_below_tier_3_fail(self) -> None:
        bad = _row(phrase="прийом бісопрололу п’ять міліграм", risk_flags=("dose",), tier=2)
        errors = validate_release_csv(_release_csv([bad]))
        assert any(e.check == "risk_tier" for e in errors)

    def test_anonymous_rows_fail_provenance(self) -> None:
        errors = validate_release_csv(_release_csv([_row(source_ref=None, review_engine=None)]))
        assert {e.check for e in errors} >= {"provenance"}

    def test_language_id_catches_english_in_uk(self) -> None:
        errors = validate_release_csv(_release_csv([_row(phrase="the patient is doing well today")]))
        assert any(e.check == "language_id" for e in errors)

    def test_code_switched_drug_name_is_fine_in_uk(self) -> None:
        errors = validate_release_csv(
            _release_csv([_row(phrase="призначено aspirin після їжі щоденно")])
        )
        assert not any(e.check == "language_id" for e in errors)

    def test_ascii_apostrophe_fails_uk(self) -> None:
        errors = validate_release_csv(_release_csv([_row(phrase="сім'я хворого поінформована")]))
        assert any(e.check == "apostrophe" for e in errors)

    def test_near_duplicates_fail(self) -> None:
        rows = [_row(), _row(phrase="загальний стан хворого задовільна")]
        errors = validate_release_csv(_release_csv(rows))
        assert any(e.check == "near_duplicate" for e in errors)

    def test_pii_fails(self) -> None:
        errors = validate_release_csv(_release_csv([_row(phrase="телефонувати на 0501234567")]))
        assert any(e.check == "pii" for e in errors)
