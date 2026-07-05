"""DevPasswordProvider — dev-tier scaffold behaviour + production refusal."""

from __future__ import annotations

import hashlib

import pytest
from medical_kep import (
    AccountLockedError,
    DevPasswordProvider,
    InlineCredentials,
    InvalidCredentialsError,
    PasswordCheckResult,
    ProviderName,
    ProviderTransientError,
    SignatureLevel,
    SignerIdentity,
)

from secret import Secret

CANONICAL = b'{"canonical_version":"1.0","probe":"dev"}'


def _verifier(result: PasswordCheckResult):
    calls: list[tuple[str, str]] = []

    async def verify(username: str, password: str) -> PasswordCheckResult:
        calls.append((username, password))
        return result

    verify.calls = calls  # type: ignore[attr-defined]
    return verify


def _signer() -> SignerIdentity:
    return SignerIdentity(
        sub="00000000-0000-0000-0000-000000000001",
        display_name="Лікар Тестовий",
        username="clinician@tenant-a.example",
    )


def _credentials(password: str = "dev-password") -> InlineCredentials:
    return InlineCredentials(dev_password=Secret(password))


# ── Production refusal (guard 1 of 3) ───────────────────────────────


@pytest.mark.parametrize("env", ["production", "prod", "PRODUCTION"])
def test_constructor_refuses_production(env: str) -> None:
    with pytest.raises(RuntimeError, match="NOT run in production"):
        DevPasswordProvider(
            password_verifier=_verifier(PasswordCheckResult.OK), environment=env
        )


def test_constructor_accepts_development() -> None:
    p = DevPasswordProvider(
        password_verifier=_verifier(PasswordCheckResult.OK), environment="development"
    )
    assert p.name is ProviderName.DEV_PASSWORD
    assert p.signature_level is SignatureLevel.DEV


# ── sign_inline outcomes ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_correct_password_produces_dev_envelope() -> None:
    verify = _verifier(PasswordCheckResult.OK)
    p = DevPasswordProvider(password_verifier=verify, environment="development")
    env = await p.sign_inline(
        canonical_bytes=CANONICAL, credentials=_credentials(), signer=_signer()
    )
    assert env.signature_level is SignatureLevel.DEV
    assert env.provider is ProviderName.DEV_PASSWORD
    assert env.parsed.is_qualified is False
    assert env.parsed.cert_chain_pem == []
    assert env.parsed.document_hash_sha256 == hashlib.sha256(CANONICAL).digest()
    # The password reached the verifier exactly once, for the JWT identity.
    assert verify.calls == [("clinician@tenant-a.example", "dev-password")]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_artifact_pdf_becomes_signed_bytes() -> None:
    p = DevPasswordProvider(
        password_verifier=_verifier(PasswordCheckResult.OK), environment="development"
    )
    env = await p.sign_inline(
        canonical_bytes=CANONICAL,
        credentials=_credentials(),
        signer=_signer(),
        artifact_pdf=b"%PDF-dev-watermarked",
    )
    assert env.signed_bytes == b"%PDF-dev-watermarked"


@pytest.mark.asyncio
async def test_wrong_password_raises_invalid_credentials() -> None:
    p = DevPasswordProvider(
        password_verifier=_verifier(PasswordCheckResult.WRONG_PASSWORD),
        environment="development",
    )
    with pytest.raises(InvalidCredentialsError):
        await p.sign_inline(
            canonical_bytes=CANONICAL, credentials=_credentials("nope"), signer=_signer()
        )


@pytest.mark.asyncio
async def test_locked_account_raises_locked() -> None:
    p = DevPasswordProvider(
        password_verifier=_verifier(PasswordCheckResult.LOCKED), environment="development"
    )
    with pytest.raises(AccountLockedError):
        await p.sign_inline(
            canonical_bytes=CANONICAL, credentials=_credentials(), signer=_signer()
        )


@pytest.mark.asyncio
async def test_idp_unavailable_raises_transient() -> None:
    p = DevPasswordProvider(
        password_verifier=_verifier(PasswordCheckResult.UNAVAILABLE),
        environment="development",
    )
    with pytest.raises(ProviderTransientError):
        await p.sign_inline(
            canonical_bytes=CANONICAL, credentials=_credentials(), signer=_signer()
        )


@pytest.mark.asyncio
async def test_missing_password_rejected_without_idp_call() -> None:
    verify = _verifier(PasswordCheckResult.OK)
    p = DevPasswordProvider(password_verifier=verify, environment="development")
    with pytest.raises(InvalidCredentialsError):
        await p.sign_inline(
            canonical_bytes=CANONICAL, credentials=InlineCredentials(), signer=_signer()
        )
    assert verify.calls == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_missing_username_rejected() -> None:
    p = DevPasswordProvider(
        password_verifier=_verifier(PasswordCheckResult.OK), environment="development"
    )
    signer = SignerIdentity(sub="0" * 36, display_name="X", username=None)
    with pytest.raises(InvalidCredentialsError):
        await p.sign_inline(
            canonical_bytes=CANONICAL, credentials=_credentials(), signer=signer
        )
