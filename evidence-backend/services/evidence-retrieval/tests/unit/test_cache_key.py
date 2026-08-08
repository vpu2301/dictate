"""cache_key: deterministic over the determinism contract, insensitive to dict order."""

from __future__ import annotations

from typing import Any

from evidence_retrieval.domain.cache import cache_key


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "query": "лікування АГ",
        "k": 8,
        "filters": {"authority": ["national"], "jurisdiction": "UA"},
        "snapshot_id": "3a2b1c0d-0000-4000-8000-000000000001",
        "tenant_id": "00000000-0000-4000-8000-0000000000aa",
    }
    payload.update(overrides)
    return payload


def test_identical_payloads_produce_identical_keys() -> None:
    key_a = cache_key(_payload(), lexicon_version="1.0", pins="bge-m3@1")
    key_b = cache_key(_payload(), lexicon_version="1.0", pins="bge-m3@1")

    assert key_a == key_b
    assert key_a.startswith("eva:retrieve:")


def test_any_payload_field_change_changes_the_key() -> None:
    base = cache_key(_payload(), lexicon_version="1.0", pins="bge-m3@1")
    variants = [
        _payload(query="лікування ЦД"),
        _payload(k=16),
        _payload(filters={"authority": ["international"], "jurisdiction": "UA"}),
        _payload(snapshot_id="3a2b1c0d-0000-4000-8000-000000000002"),
        _payload(tenant_id="00000000-0000-4000-8000-0000000000bb"),
    ]

    keys = [cache_key(v, lexicon_version="1.0", pins="bge-m3@1") for v in variants]

    assert base not in keys
    assert len(set(keys)) == len(keys)


def test_lexicon_version_and_pins_participate_in_the_key() -> None:
    base = cache_key(_payload(), lexicon_version="1.0", pins="bge-m3@1")

    assert cache_key(_payload(), lexicon_version="1.1", pins="bge-m3@1") != base
    assert cache_key(_payload(), lexicon_version="1.0", pins="bge-m3@2") != base


def test_dict_insertion_order_does_not_matter() -> None:
    ordered = {
        "query": "CKD dosing",
        "k": 8,
        "filters": {"authority": ["national"], "jurisdiction": "UA"},
        "snapshot_id": None,
        "tenant_id": "00000000-0000-4000-8000-0000000000aa",
    }
    reordered = {
        "tenant_id": "00000000-0000-4000-8000-0000000000aa",
        "snapshot_id": None,
        "filters": {"jurisdiction": "UA", "authority": ["national"]},
        "k": 8,
        "query": "CKD dosing",
    }

    assert cache_key(ordered, lexicon_version="1.0", pins="p") == cache_key(
        reordered, lexicon_version="1.0", pins="p"
    )
