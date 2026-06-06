"""Versioned binary serialisation for ``TenantTrie``.

Format header:
    bytes 0..3   magic   = b"MDXT"
    byte  4      version = 1
    byte  5      algo    = 0 (msgpack-ish JSON in sprint-10)
    bytes 6..    payload (gzip(json))

Mismatch on magic / version raises ``SerializerVersionMismatch`` and
the caller treats the cache entry as missing.
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timezone

from autocomplete_service.trie.builder import PhraseTrieEntry, TenantTrie

MAGIC = b"MDXT"
VERSION = 1
ALGO_JSONGZ = 0


class SerializerVersionMismatch(Exception):
    pass


def serialize_trie(trie: TenantTrie) -> bytes:
    payload = {
        "tenant_id": trie.tenant_id,
        "language": trie.language,
        "user_id": trie.user_id,
        "built_at_unix": trie.built_at_unix,
        "prefix_to_ids": trie.prefix_to_ids,
        "entries": {
            eid: {
                "id": e.id,
                "phrase": e.phrase,
                "source": e.source,
                "impression_count": e.impression_count,
                "acceptance_count": e.acceptance_count,
                "last_accepted_at": (
                    e.last_accepted_at.isoformat() if e.last_accepted_at else None
                ),
                "specialty": e.specialty,
                "section_hint": e.section_hint,
            }
            for eid, e in trie.entries.items()
        },
    }
    raw = gzip.compress(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    return MAGIC + bytes([VERSION, ALGO_JSONGZ]) + raw


def deserialize_trie(blob: bytes) -> TenantTrie:
    if len(blob) < 6 or blob[:4] != MAGIC:
        raise SerializerVersionMismatch("bad magic")
    version = blob[4]
    algo = blob[5]
    if version != VERSION:
        raise SerializerVersionMismatch(f"version {version}, expected {VERSION}")
    if algo != ALGO_JSONGZ:
        raise SerializerVersionMismatch(f"algo {algo}, expected {ALGO_JSONGZ}")
    raw = gzip.decompress(blob[6:])
    obj = json.loads(raw.decode("utf-8"))
    entries: dict[str, PhraseTrieEntry] = {}
    for eid, e in obj["entries"].items():
        last = e["last_accepted_at"]
        entries[eid] = PhraseTrieEntry(
            id=e["id"],
            phrase=e["phrase"],
            source=e["source"],
            impression_count=int(e["impression_count"]),
            acceptance_count=int(e["acceptance_count"]),
            last_accepted_at=datetime.fromisoformat(last) if last else None,
            specialty=e.get("specialty"),
            section_hint=e.get("section_hint"),
        )
    return TenantTrie(
        tenant_id=obj["tenant_id"],
        language=obj["language"],
        user_id=obj["user_id"],
        prefix_to_ids=obj["prefix_to_ids"],
        entries=entries,
        built_at_unix=float(obj["built_at_unix"]),
    )
