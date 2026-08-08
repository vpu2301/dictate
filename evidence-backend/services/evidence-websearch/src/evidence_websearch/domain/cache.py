"""Tenant-scoped page cache: a `web_pages` row plus two objects.

Why a cache at all: **reopen must not re-fetch** (AC-S04-B-6). An answer that
cited a page in March has to render the same text in June, whether or not the
page still exists — and re-fetching would also leak "this clinician reopened
that answer" to the publisher.

Two objects per page:
  `web/snapshot/<hash>` — the raw bytes as fetched (the evidence).
  `web/extract/<hash>`  — the extracted text (what the chunker/citer used).

Both go through `libs/storage.EncryptedObjectStore` (rule E3). Web pages are
not PHI, but there is exactly one sanctioned blob path and reusing it means
the cache inherits envelope encryption and tenant AAD for free.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from storage import EncryptedObjectStore


def normalize_url(url: str) -> str:
    """Canonical form for the cache key: lowercase host, no fragment, no
    default port, empty path becomes '/'. Query is preserved — for many
    guideline sites it selects the document."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").casefold()
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme.casefold(), host, parts.path or "/", parts.query, ""))


def url_hash(url: str) -> str:
    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CachedPage:
    url: str
    domain: str
    title: str
    text: str
    fetched_at_iso: str


class PageCache:
    def __init__(self, store: EncryptedObjectStore) -> None:
        self._store = store

    @staticmethod
    def snapshot_key(digest: str) -> str:
        return f"web/snapshot/{digest}"

    @staticmethod
    def extract_key(digest: str) -> str:
        return f"web/extract/{digest}.txt"

    async def put_snapshot(self, *, digest: str, body: bytes, tenant_id: UUID) -> str:
        key = self.snapshot_key(digest)
        await self._store.put(key=key, plaintext=body, tenant_id=tenant_id)
        return key

    async def put_extract(self, *, digest: str, text: str, tenant_id: UUID) -> str:
        key = self.extract_key(digest)
        await self._store.put(key=key, plaintext=text.encode("utf-8"), tenant_id=tenant_id)
        return key

    async def get_extract(self, *, key: str, tenant_id: UUID) -> str:
        raw = await self._store.get(key=key, tenant_id=tenant_id)
        return raw.decode("utf-8")

    async def get_snapshot(self, *, key: str, tenant_id: UUID) -> bytes:
        return await self._store.get(key=key, tenant_id=tenant_id)
