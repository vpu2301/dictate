"""libs/storage — encrypted object I/O.

Public surface:

- :class:`S3Client`              — thin async wrapper around aioboto3.
- :class:`EncryptedObjectStore`  — the ONLY sanctioned write/read path for
                                   tenant-bearing object data. Wraps every
                                   byte in libs/crypto's envelope.

There is no ``put_plaintext`` method by design. CI greps the codebase for
direct ``boto3``/``aioboto3``/``minio`` imports outside ``libs/storage`` to
prevent bypass.
"""

from __future__ import annotations

from .object_store import (
    EncryptedObjectStore,
    ObjectHeader,
    ObjectStoreDisabledError,
)
from .s3_client import S3Client

__all__ = [
    "EncryptedObjectStore",
    "ObjectHeader",
    "ObjectStoreDisabledError",
    "S3Client",
]
