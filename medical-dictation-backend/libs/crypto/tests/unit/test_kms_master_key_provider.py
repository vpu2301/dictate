"""KmsMasterKeyProvider (Vault Transit) + CompositeMasterKeyProvider tests.

A minimal in-memory Vault Transit fake behind ``httpx.MockTransport``
exercises the real HTTP contract (paths, token header, base64 framing,
``vault:vN:`` ciphertext) without a Vault binary. The live acid test —
re-wrap against a real Vault dev server — is the sprint-16 VERIFY step,
run separately (docs/runbooks/kms.md).
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import httpx
import pytest

from crypto import (
    CompositeMasterKeyProvider,
    FileMasterKeyProvider,
    KmsMasterKeyProvider,
    MasterKeyError,
    build_master_key_provider,
    fetch_kv_secrets,
)
from crypto.master import MASTER_KEY_SIZE_BYTES
from secret import Secret


class FakeTransit:
    """In-memory Transit engine: XOR 'encryption' with version framing.

    Not cryptography — a wire-contract double. Tracks calls so tests can
    assert the token header and payload framing.
    """

    def __init__(self, *, token: str = "root", mount: str = "transit", key: str = "mdx-master") -> None:
        self.token = token
        self.mount = mount
        self.key = key
        self.pad = os.urandom(64)
        self.calls: list[str] = []
        self.fail_all = False

    def _xor(self, raw: bytes) -> bytes:
        return bytes(b ^ self.pad[i % len(self.pad)] for i, b in enumerate(raw))

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.fail_all:
            raise httpx.ConnectError("vault down", request=request)
        if request.headers.get("X-Vault-Token") != self.token:
            return httpx.Response(403, json={"errors": ["permission denied"]})
        path = request.url.path
        body = json.loads(request.content)
        self.calls.append(path)
        if path == f"/v1/{self.mount}/encrypt/{self.key}":
            raw = base64.b64decode(body["plaintext"])
            ct = "vault:v1:" + base64.b64encode(self._xor(raw)).decode()
            return httpx.Response(200, json={"data": {"ciphertext": ct, "key_version": 1}})
        if path == f"/v1/{self.mount}/decrypt/{self.key}":
            ct = body["ciphertext"]
            if not ct.startswith("vault:v1:"):
                return httpx.Response(400, json={"errors": ["invalid ciphertext"]})
            raw = self._xor(base64.b64decode(ct[len("vault:v1:") :]))
            return httpx.Response(
                200, json={"data": {"plaintext": base64.b64encode(raw).decode()}}
            )
        return httpx.Response(404, json={"errors": [f"no handler for {path}"]})


@pytest.fixture
def transit() -> FakeTransit:
    return FakeTransit()


def _provider(transit: FakeTransit, **kwargs: object) -> KmsMasterKeyProvider:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(transit.handler), base_url="http://vault.local"
    )
    defaults: dict[str, object] = {
        "addr": "http://vault.local",
        "token": Secret("root"),
        "key_name": transit.key,
        "mount": transit.mount,
        "http_client": client,
    }
    defaults.update(kwargs)
    return KmsMasterKeyProvider(**defaults)  # type: ignore[arg-type]


async def test_self_check_round_trips(transit: FakeTransit) -> None:
    provider = _provider(transit)
    await provider.startup_self_check()
    assert transit.calls == [
        "/v1/transit/encrypt/mdx-master",
        "/v1/transit/decrypt/mdx-master",
    ]


async def test_self_check_fails_closed_when_unreachable(transit: FakeTransit) -> None:
    transit.fail_all = True
    provider = _provider(transit)
    with pytest.raises(MasterKeyError, match="unreachable"):
        await provider.startup_self_check()


async def test_self_check_fails_closed_on_bad_token(transit: FakeTransit) -> None:
    provider = _provider(transit, token=Secret("wrong"))
    with pytest.raises(MasterKeyError, match="403"):
        await provider.startup_self_check()


async def test_empty_token_refused_at_construction(transit: FakeTransit) -> None:
    with pytest.raises(MasterKeyError, match="token is empty"):
        _provider(transit, token=Secret(""))


async def test_wrap_unwrap_round_trip(transit: FakeTransit) -> None:
    provider = _provider(transit)
    kek = os.urandom(MASTER_KEY_SIZE_BYTES)
    master_key_id, wrapped = await provider.wrap(kek)
    assert master_key_id == "vault:transit:mdx-master"
    assert wrapped.decode().startswith("vault:v1:")
    assert await provider.unwrap(master_key_id, wrapped) == kek


async def test_wrap_rejects_non_32_byte_kek(transit: FakeTransit) -> None:
    provider = _provider(transit)
    with pytest.raises(MasterKeyError, match="32 bytes"):
        await provider.wrap(b"short")


async def test_unwrap_rejects_foreign_master_id(transit: FakeTransit) -> None:
    provider = _provider(transit)
    with pytest.raises(MasterKeyError, match="not handled"):
        await provider.unwrap("file-v1", b"vault:v1:xxxx")


# ── Composite: mixed-master reads during the re-wrap window ─────────────


@pytest.fixture
def file_key(tmp_path: Path) -> Path:
    p = tmp_path / "master.key"
    p.write_bytes(os.urandom(MASTER_KEY_SIZE_BYTES))
    os.chmod(p, 0o400)
    return p


async def test_composite_routes_unwrap_by_master_id(
    transit: FakeTransit, file_key: Path
) -> None:
    file_provider = FileMasterKeyProvider(path=file_key)
    await file_provider.startup_self_check()
    vault_provider = _provider(transit)
    composite = CompositeMasterKeyProvider(
        primary=vault_provider, fallbacks=(file_provider,)
    )

    kek = os.urandom(MASTER_KEY_SIZE_BYTES)
    file_id, file_wrapped = await file_provider.wrap(kek)
    vault_id, vault_wrapped = await composite.wrap(kek)

    # New wraps go to the primary (vault); old file rows still unwrap.
    assert vault_id == "vault:transit:mdx-master"
    assert await composite.unwrap(file_id, file_wrapped) == kek
    assert await composite.unwrap(vault_id, vault_wrapped) == kek


async def test_composite_unknown_master_id_is_precise(transit: FakeTransit) -> None:
    composite = CompositeMasterKeyProvider(primary=_provider(transit))
    with pytest.raises(MasterKeyError, match="no configured master-key provider"):
        await composite.unwrap("file-v1", b"\x00" * 60)


async def test_composite_self_check_fails_closed_on_primary(
    transit: FakeTransit, file_key: Path
) -> None:
    transit.fail_all = True
    composite = CompositeMasterKeyProvider(
        primary=_provider(transit), fallbacks=(FileMasterKeyProvider(path=file_key),)
    )
    with pytest.raises(MasterKeyError):
        await composite.startup_self_check()


async def test_composite_tolerates_broken_fallback(
    transit: FakeTransit, tmp_path: Path
) -> None:
    # Fallback file missing → warn, not fail: it only serves legacy rows.
    composite = CompositeMasterKeyProvider(
        primary=_provider(transit),
        fallbacks=(FileMasterKeyProvider(path=tmp_path / "gone"),),
    )
    await composite.startup_self_check()  # must not raise


# ── build_master_key_provider (the sanctioned composition helper) ──────


async def test_builder_file_mode_is_plain_file_provider(file_key: Path) -> None:
    provider = build_master_key_provider(provider="file", file_path=file_key)
    assert isinstance(provider, FileMasterKeyProvider)


async def test_builder_vault_mode_includes_file_fallback_iff_present(
    file_key: Path,
) -> None:
    with_fallback = build_master_key_provider(
        provider="vault",
        file_path=file_key,
        vault_addr="http://vault.local",
        vault_token=Secret("root"),
    )
    assert isinstance(with_fallback, CompositeMasterKeyProvider)
    assert len(with_fallback.members) == 2

    pure_kms = build_master_key_provider(
        provider="vault",
        file_path=file_key.parent / "removed-after-migration",
        vault_addr="http://vault.local",
        vault_token=Secret("root"),
    )
    assert isinstance(pure_kms, CompositeMasterKeyProvider)
    assert len(pure_kms.members) == 1
    await with_fallback.aclose()
    await pure_kms.aclose()


async def test_builder_rejects_unknown_provider() -> None:
    with pytest.raises(MasterKeyError, match="unknown"):
        build_master_key_provider(provider="aws-kms", file_path="/etc/mdx/master.key")


async def test_builder_vault_requires_addr() -> None:
    with pytest.raises(MasterKeyError, match="MDX_VAULT_ADDR"):
        build_master_key_provider(provider="vault", file_path=None, vault_addr=None)


# ── fetch_kv_secrets (system HMAC keys seam) ────────────────────────────


async def test_fetch_kv_secrets_reads_kv_v2(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/secret/data/mdx/signing"
        assert request.headers["X-Vault-Token"] == "root"
        return httpx.Response(
            200,
            json={"data": {"data": {"signer_ipn_hmac_key": "aa" * 32}}},
        )

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def patched(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncClient", patched)
    out = await fetch_kv_secrets(
        addr="http://vault.local", token=Secret("root"), path="mdx/signing"
    )
    assert out == {"signer_ipn_hmac_key": "aa" * 32}


async def test_fetch_kv_secrets_fails_closed_on_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(404, json={"errors": []}))
    real_client = httpx.AsyncClient

    def patched(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncClient", patched)
    with pytest.raises(MasterKeyError, match="404"):
        await fetch_kv_secrets(
            addr="http://vault.local", token="root", path="mdx/signing"
        )
