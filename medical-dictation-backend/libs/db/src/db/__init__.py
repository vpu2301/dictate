"""libs/db — async DB utilities for the platform.

The single sanctioned way to obtain a tenant-scoped DB connection is
``tenant_connection``. There is no escape hatch in Sprint 01; one is
introduced in Sprint 02 explicitly for the audit writer.
"""

from .engine import Base, make_engine
from .icd10_query import ICD10_SEARCH_SQL
from .pool import create_pool
from .tenant import tenant_connection

__all__ = ["ICD10_SEARCH_SQL", "create_pool", "tenant_connection", "Base", "make_engine"]
