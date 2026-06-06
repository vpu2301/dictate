"""PII redactor coverage."""

from __future__ import annotations

from uuid import uuid4

from report_service.domain.pii_redactor import is_treatment_team, redact_snippet


def test_ipn_redacted():
    out = redact_snippet("патієнт ІПН 1234567890 поступив зі скаргами")
    assert "1234567890" not in out
    assert "[redacted-ipn]" in out


def test_dob_like_redacted():
    out = redact_snippet("дата народження 12.05.1980")
    assert "12.05.1980" not in out
    assert "[redacted-date]" in out


def test_pib_redacted_cyrillic():
    out = redact_snippet("оглянуто Іваненко Петро Сергійович сьогодні")
    assert "Іваненко" not in out
    assert "[redacted-name]" in out


def test_treatment_team_primary_author():
    uid = uuid4()
    assert is_treatment_team(
        viewer_user_id=uid,
        primary_author_id=uid,
        co_author_ids=[],
        viewer_roles=["clinician"],
    )


def test_treatment_team_co_author():
    primary = uuid4()
    viewer = uuid4()
    assert is_treatment_team(
        viewer_user_id=viewer,
        primary_author_id=primary,
        co_author_ids=[viewer],
        viewer_roles=["clinician"],
    )


def test_tenant_admin_treated_as_team():
    viewer = uuid4()
    other = uuid4()
    assert is_treatment_team(
        viewer_user_id=viewer,
        primary_author_id=other,
        co_author_ids=[],
        viewer_roles=["tenant_admin"],
    )


def test_random_clinician_not_on_team():
    viewer = uuid4()
    other = uuid4()
    assert not is_treatment_team(
        viewer_user_id=viewer,
        primary_author_id=other,
        co_author_ids=[],
        viewer_roles=["clinician"],
    )
