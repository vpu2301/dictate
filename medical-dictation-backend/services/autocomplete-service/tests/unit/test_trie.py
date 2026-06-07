"""Trie build + serialize roundtrip + suggest unit tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from autocomplete_service.suggest import (
    extract_snippet_trigger,
    is_snippet_prefix,
    suggest_from_trie,
)
from autocomplete_service.trie import (
    PhraseTrieEntry,
    SerializerVersionMismatchError,
    build_trie_from_phrases,
    deserialize_trie,
    serialize_trie,
)


def _entry(id_: str, phrase: str, **kw) -> PhraseTrieEntry:
    return PhraseTrieEntry(
        id=id_,
        phrase=phrase,
        source=kw.get("source", "system"),
        impression_count=kw.get("imp", 0),
        acceptance_count=kw.get("acc", 0),
        last_accepted_at=kw.get("last"),
        specialty=kw.get("specialty"),
        section_hint=kw.get("hint"),
    )


def test_build_indexes_short_prefixes():
    trie = build_trie_from_phrases(
        tenant_id="t",
        language="uk",
        user_id="u",
        rows=[
            _entry("a", "задишка при навантаженні"),
            _entry("b", "задишка спокою"),
            _entry("c", "біль у грудях"),
        ],
    )
    assert "за" in trie.prefix_to_ids
    cands = trie.candidates_for("задиш")
    ids = {c.id for c in cands}
    assert ids == {"a", "b"}


def test_serializer_roundtrip_preserves_entries():
    trie = build_trie_from_phrases(
        tenant_id="t",
        language="uk",
        user_id="u",
        rows=[
            _entry("x", "ритм синусовий", source="user", imp=5, acc=3, last=datetime.now(UTC)),
            _entry("y", "тони серця ясні", source="tenant"),
        ],
    )
    blob = serialize_trie(trie)
    restored = deserialize_trie(blob)
    assert set(restored.entries.keys()) == {"x", "y"}
    assert restored.entries["x"].source == "user"
    assert restored.entries["x"].impression_count == 5


def test_serializer_rejects_bad_magic():
    with pytest.raises(SerializerVersionMismatchError):
        deserialize_trie(b"BAD" + b"\x01\x00")


def test_serializer_rejects_unknown_version():
    blob = b"MDXT" + bytes([99, 0])
    with pytest.raises(SerializerVersionMismatchError):
        deserialize_trie(blob)


def test_is_snippet_prefix_detects_slash():
    assert is_snippet_prefix("/cv")
    assert not is_snippet_prefix("задишк")


def test_extract_snippet_trigger_lowercases():
    assert extract_snippet_trigger("/CV") == "cv"
    assert extract_snippet_trigger("//vitals") == "vitals"


def test_suggest_from_trie_returns_top_k():
    # Phrases differ in suffix by > Levenshtein 3 so the diversity
    # guard does not collapse them.
    trie = build_trie_from_phrases(
        tenant_id="t",
        language="uk",
        user_id="u",
        rows=[
            _entry("a", "задишка при навантаженні", source="user", imp=10, acc=8),
            _entry("b", "задишка в спокої вночі", source="tenant", imp=10, acc=4),
            _entry("c", "задишка змішаного характеру", source="system", imp=10, acc=2),
            _entry("d", "не починається з задишк", source="system"),
        ],
    )
    out = suggest_from_trie(trie=trie, prefix="задиш", limit=3)
    assert len(out) == 3
    # User-source highest priority should rank first.
    assert out[0].id == "a"
    assert all(s.kind == "phrase" for s in out)
    # `completion` strips the matched prefix.
    assert out[0].completion.startswith("ка при")


def test_diversity_guard_collapses_near_duplicates():
    trie = build_trie_from_phrases(
        tenant_id="t",
        language="uk",
        user_id="u",
        rows=[
            _entry("a", "задишка тип 1", source="user", imp=10, acc=8),
            _entry("b", "задишка тип 2", source="tenant", imp=10, acc=4),
            _entry("c", "задишка тип 3", source="system", imp=10, acc=2),
        ],
    )
    out = suggest_from_trie(trie=trie, prefix="задиш", limit=3)
    # All three suffixes are within Levenshtein 3 → only the best survives.
    assert len(out) == 1
    assert out[0].id == "a"


def test_suggest_from_trie_empty_on_unknown_prefix():
    trie = build_trie_from_phrases(
        tenant_id="t",
        language="uk",
        user_id="u",
        rows=[_entry("a", "інше")],
    )
    assert suggest_from_trie(trie=trie, prefix="zzzzzzz", limit=3) == []
