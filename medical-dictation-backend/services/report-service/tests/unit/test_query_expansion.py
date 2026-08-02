"""Query-expansion assembly — pure logic, no DB."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from report_service.domain.query_expansion import (
    MAX_EXPANSIONS,
    ExpandedQuery,
    assemble_tsquery,
)
from report_service.domain.search import SearchFilters, _fts_clause

G_IM = uuid4()
G_AT = uuid4()


def _row(group_id, term, lexemes) -> dict[str, Any]:
    return {"group_id": group_id, "term": term, "lexemes": lexemes}


IM_ROWS = [
    _row(G_IM, "ІМ", ["ім"]),
    _row(G_IM, "інфаркт міокарда", ["інфаркт", "міокарда"]),
    _row(G_IM, "MI", ["mi"]),
]


def test_single_abbreviation_expands_to_or_group():
    result = assemble_tsquery(["ім"], IM_ROWS)
    assert result.tsquery == "('ім' | ('інфаркт' & 'міокарда') | 'mi')"
    assert result.groups_used == 1
    assert "інфаркт міокарда" in result.expanded_terms


def test_full_form_expands_back_to_abbreviation():
    result = assemble_tsquery(["інфаркт", "міокарда"], IM_ROWS)
    # Each lexeme of the multi-word form gets the group's alternatives —
    # a document containing only «ІМ» satisfies both conjuncts.
    assert result.tsquery == (
        "('інфаркт' | 'ім' | ('інфаркт' & 'міокарда') | 'mi')"
        " & "
        "('міокарда' | 'ім' | ('інфаркт' & 'міокарда') | 'mi')"
    )
    assert result.groups_used == 1


def test_unmatched_lexemes_stay_plain():
    result = assemble_tsquery(["головний", "біль"], IM_ROWS)
    assert result.tsquery == "'головний' & 'біль'"
    assert result.groups_used == 0
    assert result.expanded_terms == []


def test_short_lexemes_never_expand():
    # «в» must not drag in the в/в = внутрішньовенно group.
    rows = [_row(G_AT, "в/в", ["в"]), _row(G_AT, "внутрішньовенно", ["внутрішньовенно"])]
    result = assemble_tsquery(["в", "грудях"], rows)
    assert result.tsquery == "'в' & 'грудях'"
    assert result.groups_used == 0


def test_apostrophe_lexemes_quoted_safely():
    rows = [
        _row(G_AT, "м'язовий біль", ["м'язовий", "біль"]),
        _row(G_AT, "міалгія", ["міалгія"]),
    ]
    result = assemble_tsquery(["міалгія"], rows)
    assert result.tsquery == "('міалгія' | ('м''язовий' & 'біль'))"


def test_expansion_cap_holds_on_adversarial_query():
    rows = []
    lexemes = []
    for i in range(MAX_EXPANSIONS + 4):
        gid = uuid4()
        lex = f"абр{i}"
        lexemes.append(lex)
        rows.append(_row(gid, f"АБР{i}", [lex]))
        rows.append(_row(gid, f"повна форма {i}", [f"повна{i}", f"форма{i}"]))
    result = assemble_tsquery(lexemes, rows)
    assert result.tsquery is not None
    # Exactly MAX_EXPANSIONS lexemes got an OR-group; the overflow stayed
    # plain. An expanded conjunct contains the group's full form; a plain
    # one is just the quoted lexeme.
    or_groups = result.tsquery.count(" | ")
    assert result.groups_used == MAX_EXPANSIONS
    assert or_groups == MAX_EXPANSIONS
    for i in range(MAX_EXPANSIONS, MAX_EXPANSIONS + 4):
        assert f"'абр{i}'" in result.tsquery
        assert f"повна{i}" not in result.tsquery


def test_empty_lexemes_yield_none():
    assert assemble_tsquery([], IM_ROWS) == ExpandedQuery(tsquery=None)


def test_same_term_in_two_groups_unions():
    # ГКС: гострий коронарний синдром AND глюкокортикостероїди — deliberate.
    g_acs, g_gcs = uuid4(), uuid4()
    rows = [
        _row(g_acs, "ГКС", ["гкс"]),
        _row(g_acs, "гострий коронарний синдром", ["гострий", "коронарний", "синдром"]),
        _row(g_gcs, "ГКС", ["гкс"]),
        _row(g_gcs, "глюкокортикостероїди", ["глюкокортикостероїди"]),
    ]
    result = assemble_tsquery(["гкс"], rows)
    assert result.tsquery is not None
    assert "'коронарний'" in result.tsquery
    assert "'глюкокортикостероїди'" in result.tsquery
    assert result.groups_used == 2


# ── search.py FTS clause selection ──────────────────────────────────


def test_fts_clause_plainto_without_expansion():
    args: list = []
    clause = _fts_clause(SearchFilters(q="ІМ"), args)
    assert clause is not None
    assert clause[0] == "v.search_vector @@ plainto_tsquery('simple', $1)"
    assert args == ["ІМ"]


def test_fts_clause_to_tsquery_with_expansion():
    args: list = ["prior-arg"]
    filters = SearchFilters(q="ІМ", ts_query="('ім' | 'mi')")
    clause = _fts_clause(filters, args)
    assert clause is not None
    assert clause[0] == "v.search_vector @@ to_tsquery('simple', $2)"
    assert clause[1] == "to_tsquery('simple', $2)"
    assert args == ["prior-arg", "('ім' | 'mi')"]


def test_fts_clause_absent_without_q():
    args: list = []
    assert _fts_clause(SearchFilters(), args) is None
    assert args == []
