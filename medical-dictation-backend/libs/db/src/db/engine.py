"""SQLAlchemy async engine factory and shared declarative base.

asyncpg is preferred for hot-path tenant-scoped queries via ``tenant_connection``;
this module exists for ORM-driven services that need SQLAlchemy.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Shared declarative base for all service models."""


def make_engine(database_url: str, **kwargs: object) -> AsyncEngine:
    """Create an async SQLAlchemy engine.

    ``database_url`` must be asyncpg-compatible, e.g.
    ``postgresql+asyncpg://user:pass@host/db``.
    """
    return create_async_engine(database_url, **kwargs)
