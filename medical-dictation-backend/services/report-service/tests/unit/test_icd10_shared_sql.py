"""The picker endpoint and the extractor must rank identically.

Both import ``db.ICD10_SEARCH_SQL``; this pins that neither grew a
private copy. If the picker showed one "best match" and the extractor
proposed another, clinicians would see the system contradict itself.
"""

from __future__ import annotations

from pathlib import Path

from db import ICD10_SEARCH_SQL
from report_service.routers import icd10 as icd10_router

_REPO = Path(__file__).resolve().parents[4]
_EXTRACTOR_REPO = (
    _REPO / "services" / "nlp-service" / "src" / "nlp_service" / "stages" / "icd10_repository.py"
)


def test_router_uses_the_shared_constant() -> None:
    assert icd10_router.ICD10_SEARCH_SQL is ICD10_SEARCH_SQL


def test_extractor_repository_imports_the_shared_constant() -> None:
    source = _EXTRACTOR_REPO.read_text("utf-8")
    assert "from db import ICD10_SEARCH_SQL" in source


def test_no_second_copy_of_the_ranking_sql_exists() -> None:
    """No service may inline the ranking query — it must be imported."""
    marker = "FROM icd10_codes c"
    hits = [
        str(path.relative_to(_REPO))
        for path in (_REPO / "services").rglob("src/**/*.py")
        if "__pycache__" not in str(path) and marker in path.read_text("utf-8")
    ]
    assert hits == [], f"ranking SQL duplicated in {hits}"


def test_shared_sql_ranks_exact_then_prefix_then_fts() -> None:
    """The tier ladder is the contract; guard against a silent reorder."""
    normalized = " ".join(ICD10_SEARCH_SQL.split())
    assert "WHEN c.code = q.code_q THEN 0" in normalized
    assert "WHEN c.code LIKE q.code_q || '%' THEN 1" in normalized
    assert "ELSE 2" in normalized
    assert "ORDER BY tier ASC, c.is_leaf DESC, rank DESC, c.code ASC" in normalized
