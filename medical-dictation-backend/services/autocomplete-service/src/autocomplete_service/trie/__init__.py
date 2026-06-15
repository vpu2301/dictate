"""Trie + Redis cache for the autocomplete hot path."""

from autocomplete_service.trie.builder import (
    PhraseTrieEntry,
    TenantTrie,
    build_trie_from_phrases,
)
from autocomplete_service.trie.cache import TrieCache
from autocomplete_service.trie.serializer import (
    SerializerVersionMismatchError,
    deserialize_trie,
    serialize_trie,
)

__all__ = [
    "PhraseTrieEntry",
    "SerializerVersionMismatchError",
    "TenantTrie",
    "TrieCache",
    "build_trie_from_phrases",
    "deserialize_trie",
    "serialize_trie",
]
