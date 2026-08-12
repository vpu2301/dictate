"""Password strength rules.

The cases that matter are the ones a naive length check would wave
through: a blocklisted password wearing a suffix, the user's own email
local part, and leet substitution.
"""

from __future__ import annotations

import pytest

from auth_service.domain import password_policy as pol


def test_accepts_a_long_passphrase() -> None:
    result = pol.check_password("correct horse battery staple")
    assert result.ok
    assert result.reasons == ()
    assert result.score >= 3


def test_rejects_short_even_when_complex() -> None:
    # The exact shape a composition rule would happily accept.
    result = pol.check_password("Aa1!Bb2@")
    assert not result.ok
    assert pol.TOO_SHORT in result.reasons


def test_no_composition_rule_is_imposed() -> None:
    """A long all-lowercase passphrase must pass.

    NIST SP 800-63B §5.1.1.2 says verifiers SHOULD NOT require character
    variety. If this test ever fails, someone has added a rule the spec
    explicitly warns against.
    """
    assert pol.check_password("thequickbrownfoxjumpsover").ok


@pytest.mark.parametrize(
    "password",
    [
        "password1234",
        "Passw0rd1234",
        "p@ssw0rd1234",  # leet substitution of a blocklisted word
        "qwerty123456",
        "changeme1234",
        "dev-password",
    ],
)
def test_rejects_blocklisted_and_derived(password: str) -> None:
    result = pol.check_password(password)
    assert not result.ok, password
    assert pol.COMMON in result.reasons or pol.SEQUENTIAL in result.reasons


def test_rejects_password_containing_email_local_part() -> None:
    result = pol.check_password(
        "kovalenko-is-here-now", email="olena.kovalenko@clinic.example"
    )
    assert not result.ok
    assert pol.CONTAINS_IDENTIFIER in result.reasons


def test_rejects_password_containing_display_name() -> None:
    result = pol.check_password("Kateryna!Kateryna", display_name="Kateryna Bondar")
    assert not result.ok
    assert pol.CONTAINS_IDENTIFIER in result.reasons


def test_short_identifier_fragments_do_not_block() -> None:
    """A two-letter fragment must not ban half the dictionary.

    `de@x.com` yields the fragment "de"; banning it would reject an
    enormous share of legitimate German passphrases for no gain.
    """
    assert pol.check_password("wanderlust morning", email="de@x.com").ok


def test_rejects_sequential_runs() -> None:
    assert not pol.check_password("abcdefghijklmn").ok
    assert not pol.check_password("zzz12345678zzz").ok


def test_rejects_single_repeated_character() -> None:
    result = pol.check_password("aaaaaaaaaaaaaaaa")
    assert not result.ok
    assert pol.REPEATED in result.reasons


def test_rejects_whitespace_only_with_its_own_reason() -> None:
    result = pol.check_password("                    ")
    assert not result.ok
    assert result.reasons == (pol.WHITESPACE_ONLY,)


def test_rejects_over_max_length() -> None:
    result = pol.check_password("a" + "bcdefghij" * 20)
    assert not result.ok
    assert pol.TOO_LONG in result.reasons


def test_min_length_cannot_be_configured_below_the_floor() -> None:
    """A deployment may raise the bar, never drop it under 800-63B's floor."""
    result = pol.check_password("short1", min_length=2)
    assert not result.ok
    assert pol.TOO_SHORT in result.reasons


def test_min_length_can_be_raised() -> None:
    assert pol.check_password("twelve chars", min_length=12).ok
    assert not pol.check_password("twelve chars", min_length=20).ok


def test_unicode_normalisation_catches_fullwidth_blocklist_evasion() -> None:
    """Keycloak normalises on its side, so the check must too."""
    result = pol.check_password("ｐａｓｓｗｏｒｄ１２３４")
    assert not result.ok
    assert pol.COMMON in result.reasons


def test_score_is_zero_for_rejected_passwords() -> None:
    """The meter must never encourage something the gate will refuse."""
    assert pol.check_password("password1234").score == 0


def test_score_rises_with_length() -> None:
    assert pol.strength_score("elephant zoo") < pol.strength_score(
        "elephant zoo marmalade tuesday"
    )
