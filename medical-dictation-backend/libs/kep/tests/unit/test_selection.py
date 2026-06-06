"""Provider selection logic."""

from __future__ import annotations

from medical_kep.provider import ProviderName
from medical_kep.selection import select_providers


def test_diia_default_when_both_healthy():
    r = select_providers(health={ProviderName.DIIA: True, ProviderName.IIT: True})
    assert r.default == ProviderName.DIIA
    assert r.chosen == ProviderName.DIIA
    assert ProviderName.DIIA in r.available
    assert ProviderName.IIT in r.available
    assert r.unhealthy == []


def test_iit_chosen_when_diia_down():
    r = select_providers(health={ProviderName.DIIA: False, ProviderName.IIT: True})
    assert r.default == ProviderName.IIT
    assert r.chosen == ProviderName.IIT
    assert r.unhealthy == [ProviderName.DIIA]


def test_no_provider_when_both_down():
    r = select_providers(health={ProviderName.DIIA: False, ProviderName.IIT: False})
    assert r.default is None
    assert r.chosen is None
    assert r.available == []


def test_user_choice_overrides_default_when_healthy():
    r = select_providers(
        health={ProviderName.DIIA: True, ProviderName.IIT: True},
        user_choice=ProviderName.IIT,
    )
    assert r.chosen == ProviderName.IIT
    assert r.default == ProviderName.DIIA


def test_user_choice_unhealthy_falls_back_to_default():
    r = select_providers(
        health={ProviderName.DIIA: True, ProviderName.IIT: False},
        user_choice=ProviderName.IIT,
    )
    # User asked for ІІТ but it's down → default wins.
    assert r.chosen == ProviderName.DIIA


def test_mock_only_when_allowed():
    r = select_providers(
        health={ProviderName.DIIA: False, ProviderName.IIT: False, ProviderName.MOCK: True},
        allow_mock=True,
    )
    assert ProviderName.MOCK in r.available
    assert r.chosen == ProviderName.MOCK


def test_mock_excluded_by_default():
    r = select_providers(
        health={ProviderName.DIIA: False, ProviderName.IIT: False, ProviderName.MOCK: True},
    )
    assert ProviderName.MOCK not in r.available
