"""Corpus artifact storage — EncryptedObjectStore is the sanctioned path
(rule E3) even though the global corpus is not PHI."""

from __future__ import annotations

import json
from uuid import UUID

from storage import EncryptedObjectStore

from evidence_ingest.domain.parse_types import ParsedDocument


class CorpusStore:
    def __init__(self, store: EncryptedObjectStore) -> None:
        self._store = store

    @staticmethod
    def raw_key(document_ref: str, version: int) -> str:
        return f"corpus/raw/{document_ref}/v{version}"

    @staticmethod
    def parsed_key(document_ref: str, version: int) -> str:
        return f"corpus/parsed/{document_ref}/v{version}.json"

    async def put_raw(self, *, key: str, content: bytes, tenant_id: UUID) -> str:
        await self._store.put(key=key, plaintext=content, tenant_id=tenant_id)
        return f"minio://{self._store.bucket}/{key}"

    async def put_parsed(self, *, key: str, doc: ParsedDocument, tenant_id: UUID) -> str:
        payload = doc.model_dump_json().encode("utf-8")
        await self._store.put(key=key, plaintext=payload, tenant_id=tenant_id)
        return f"minio://{self._store.bucket}/{key}"

    async def get_parsed(self, *, key: str, tenant_id: UUID) -> ParsedDocument:
        raw = await self._store.get(key=key, tenant_id=tenant_id)
        return ParsedDocument.model_validate(json.loads(raw))

    async def get_raw(self, *, key: str, tenant_id: UUID) -> bytes:
        return await self._store.get(key=key, tenant_id=tenant_id)
