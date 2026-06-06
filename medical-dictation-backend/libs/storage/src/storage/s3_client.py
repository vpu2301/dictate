"""Async S3/MinIO adapter — the only place we touch ``aioboto3``.

Centralizing the client makes it trivial to swap implementations later
(e.g., to a direct ``aiohttp`` MinIO client) and means CI's grep for
``boto3`` / ``aioboto3`` imports has exactly one allowed origin.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

# aioboto3 is imported lazily inside ``_session`` so the module can be
# imported in environments where boto3/aioboto3 isn't installed (e.g.,
# the asr-service test rig that only exercises EncryptedObjectStore
# against an in-memory S3 substitute).
try:  # pragma: no cover  — import-time guard
    import aioboto3
    from botocore.exceptions import ClientError
except ImportError:  # pragma: no cover
    aioboto3 = None  # type: ignore[assignment]
    ClientError = Exception  # type: ignore[misc,assignment]

logger = logging.getLogger(__name__)


class S3Client:
    """Lazily-bound aioboto3 session + per-call client context.

    aioboto3 expects you to ``async with session.client(...) as c`` for
    each call; reusing a single client across requests is supported via
    its session pool. We expose narrow methods rather than the raw client
    so callers cannot bypass the envelope.
    """

    def __init__(
        self,
        *,
        endpoint_url: str,
        access_key: str,
        secret_key: str,
        region: str = "us-east-1",
        use_ssl: bool = False,
    ) -> None:
        self._endpoint = endpoint_url
        self._access = access_key
        self._secret = secret_key
        self._region = region
        self._use_ssl = use_ssl
        if aioboto3 is None:
            raise RuntimeError(
                "aioboto3 is not installed; S3Client cannot be constructed. "
                "Install via `uv sync` or use a mock S3 in tests."
            )
        self._session = aioboto3.Session()

    def _client(self) -> Any:
        return self._session.client(
            "s3",
            endpoint_url=self._endpoint,
            aws_access_key_id=self._access,
            aws_secret_access_key=self._secret,
            region_name=self._region,
            use_ssl=self._use_ssl,
        )

    async def put_object(self, *, bucket: str, key: str, body: bytes) -> None:
        async with self._client() as c:
            await c.put_object(
                Bucket=bucket,
                Key=key,
                Body=body,
                ContentType="application/octet-stream",
            )

    async def get_object(self, *, bucket: str, key: str) -> bytes:
        async with self._client() as c:
            resp = await c.get_object(Bucket=bucket, Key=key)
            body = resp["Body"]
            return await body.read()

    async def delete_object(self, *, bucket: str, key: str) -> None:
        async with self._client() as c:
            with contextlib.suppress(ClientError):
                await c.delete_object(Bucket=bucket, Key=key)

    async def head_bucket(self, bucket: str) -> None:
        """Probe used by readiness checks."""
        async with self._client() as c:
            await c.head_bucket(Bucket=bucket)

    async def generate_presigned_url(
        self, *, bucket: str, key: str, expires_in: int
    ) -> str:
        async with self._client() as c:
            url: str = await c.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket, "Key": key},
                ExpiresIn=expires_in,
            )
            return url

    async def aclose(self) -> None:
        # aioboto3 Session has no explicit close; per-call clients are
        # context-managed. The method exists for symmetry with other
        # libs' teardown signatures.
        return None
