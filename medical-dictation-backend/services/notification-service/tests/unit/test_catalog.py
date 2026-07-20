"""The catalog must cover the category enum exactly — no more, no less."""

from __future__ import annotations

import pytest

from notification_events import Category, EmailMode
from notification_service.domain.catalog import (
    CATALOG,
    RecipientRule,
    digest_eligible_categories,
    emailing_categories,
    spec_for,
)


def test_catalog_covers_every_category() -> None:
    """A category with no entry would notify nobody, silently."""
    assert set(CATALOG) == set(Category)


def test_catalog_has_no_orphan_entries() -> None:
    for category in CATALOG:
        assert isinstance(category, Category)


def test_spec_key_matches_its_category() -> None:
    """Guards a copy-paste error in the table itself."""
    for category, spec in CATALOG.items():
        assert spec.category is category


def test_unknown_category_raises() -> None:
    with pytest.raises(KeyError):
        spec_for("report.telepathy")  # type: ignore[arg-type]


def test_emailing_categories_have_templates() -> None:
    for category in emailing_categories():
        assert CATALOG[category].email_template


def test_non_emailing_categories_have_no_template() -> None:
    """A template that can never render is dead weight the PHI gate must not scan."""
    for category, spec in CATALOG.items():
        if spec.default_email_mode is EmailMode.OFF and not spec.email_template:
            assert category not in emailing_categories()


def test_critical_categories_are_never_digest_eligible() -> None:
    """Batching an alert into tomorrow's summary is a defect, not a preference."""
    for spec in CATALOG.values():
        if spec.severity == "critical":
            assert not spec.digest_eligible


def test_failure_categories_bypass_the_digest() -> None:
    assert Category.TRANSCRIPTION_FAILED not in digest_eligible_categories()
    assert Category.REPORT_SIGNING_FAILED not in digest_eligible_categories()
    assert Category.REPORT_CHAIN_FAILURE not in digest_eligible_categories()
    assert Category.REPORT_SIGNED not in digest_eligible_categories()


def test_chain_failure_goes_to_admins_not_clinicians() -> None:
    """An integrity failure is an operational concern (demo step 6)."""
    assert CATALOG[Category.REPORT_CHAIN_FAILURE].recipient_rule is RecipientRule.TENANT_ADMINS


def test_digest_category_does_not_notify_in_app() -> None:
    """An in-app copy of a summary of in-app notifications is noise."""
    assert CATALOG[Category.SYSTEM_DIGEST].default_in_app is False


def test_self_addressed_categories_do_not_exclude_the_actor() -> None:
    """The regression this file exists to prevent.

    These three categories are addressed to the person who caused them —
    a completion receipt for work they started and stopped watching. With
    the `exclude_actor=True` default, `resolve_recipients` strips the
    only recipient and the event materialises into zero rows: a user
    finishes a dictation, finalizes their own report, and the feed they
    are staring at stays empty. That failure is silent (a debug-level
    "no recipients" line), which is what made it survive.
    """
    for category in (
        Category.DICTATION_COMPLETED,
        Category.TRANSCRIPTION_COMPLETED,
        Category.TRANSCRIPTION_FAILED,
        Category.REPORT_FINALIZED,
        Category.REPORT_SIGNED,
    ):
        assert CATALOG[category].exclude_actor is False, category


def test_dictation_completion_never_emails() -> None:
    """One mail per dictation is the noisiest thing this service could do."""
    spec = CATALOG[Category.DICTATION_COMPLETED]
    assert spec.default_email_mode is EmailMode.OFF
    assert spec.email_template == ""
    assert Category.DICTATION_COMPLETED not in emailing_categories()


def test_dictation_completion_is_addressed_by_hint() -> None:
    """Only the dictating clinician; nobody else cares that a colleague stopped."""
    assert (
        CATALOG[Category.DICTATION_COMPLETED].recipient_rule is RecipientRule.EXPLICIT_HINTS
    )
