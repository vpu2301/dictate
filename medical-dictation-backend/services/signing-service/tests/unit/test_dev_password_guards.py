"""Production guards for the dev_password scaffold (guards 1 + 2 of 3;
guard 3 is the ``check-no-dev-signing-in-prod-config`` CI gate)."""

from __future__ import annotations

import pytest
from medical_kep import DevPasswordProvider, PasswordCheckResult
from pydantic import ValidationError
from signing_service.config import Settings


async def _verifier(username: str, password: str) -> PasswordCheckResult:
    return PasswordCheckResult.OK


# ── Guard 2: config model rejects the flag in production ────────────


def test_config_rejects_dev_password_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("SIGNING_DEV_PASSWORD_ENABLED", "true")
    with pytest.raises(ValidationError, match="never be set in production"):
        Settings(_env_file=None)


def test_config_rejects_dev_password_in_prod_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "prod")
    monkeypatch.setenv("SIGNING_DEV_PASSWORD_ENABLED", "1")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_config_allows_dev_password_in_development(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("SIGNING_DEV_PASSWORD_ENABLED", "true")
    s = Settings(_env_file=None)
    assert s.enable_dev_password_provider is True


def test_config_default_is_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SIGNING_DEV_PASSWORD_ENABLED", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "production")
    s = Settings(_env_file=None)
    assert s.enable_dev_password_provider is False


# ── Guard 1: the provider itself refuses production ─────────────────


def test_provider_constructor_refuses_production() -> None:
    with pytest.raises(RuntimeError, match="NOT run in production"):
        DevPasswordProvider(password_verifier=_verifier, environment="production")
