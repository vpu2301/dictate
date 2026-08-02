"""Synonym query expansion (sprint 15, ADR-0038).

Wraps — never forks — the sprint-08 search: when a query lexeme matches
a synonym-group term, the lexeme's tsquery atom broadens from ``'ім'``
to ``('ім' | ('інфаркт' & 'міокарда') | 'mi')``. The assembled tsquery
STRING travels as a bind parameter into ``to_tsquery('simple', $n)`` —
no SQL injection surface, and no tsquery-syntax surface either because
every atom is a lexeme that already came out of ``to_tsvector('simple')``
(the icd10_query.py normalization precedent: apostrophes in «м'язів»
would otherwise break the syntax; quoting doubles them).

Both sides of the match are normalized by the SAME Postgres config:
``medical_synonyms.lexemes`` is computed via ``to_tsvector('simple')``
at write/seed time, the query through the one roundtrip below. A
multi-word query hitting a multi-word term still works per-lexeme:
«інфаркт міокарда» → ('інфаркт'|'ім'|'mi') & ('міокарда'|'ім'|'mi') —
a document containing only «ІМ» satisfies both conjuncts.

Caps: at most ``MAX_EXPANSIONS`` query lexemes get synonym groups (the
rest stay plain — bounds cost on adversarial queries); lexemes shorter
than ``MIN_LEXEME_LEN`` never expand (the Ukrainian preposition «в»
must not drag in the в/в = внутрішньовенно group).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

import asyncpg

MAX_EXPANSIONS = 8
MIN_LEXEME_LEN = 2


@dataclass(slots=True)
class ExpandedQuery:
    # tsquery string for to_tsquery('simple', $n); None → nothing to search
    # (zero lexemes: punctuation-only input) — caller keeps the plainto path.
    tsquery: str | None
    # Canonical terms whose groups fired (for the response's transparency
    # field + metrics; closed vocabulary, never patient prose).
    expanded_terms: list[str] = field(default_factory=list)
    groups_used: int = 0


def _quote(lexeme: str) -> str:
    return "'" + lexeme.replace("'", "''") + "'"


def _term_atom(lexemes: list[str]) -> str:
    """A term's tsquery atom: single lexeme plain, multi-word AND-grouped."""
    if len(lexemes) == 1:
        return _quote(lexemes[0])
    return "(" + " & ".join(_quote(lex) for lex in lexemes) + ")"


async def normalize_lexemes(conn: asyncpg.Connection, raw: str) -> list[str]:
    """One roundtrip through the SAME config the index uses."""
    rows: list[str] | None = await conn.fetchval(
        "SELECT tsvector_to_array(to_tsvector('simple', $1))", raw
    )
    return list(rows or [])


async def fetch_matching_synonyms(
    conn: asyncpg.Connection, *, lexemes: list[str]
) -> list[asyncpg.Record]:
    """EVERY row of every group that shares a lexeme with the query — the
    assembler needs the full group membership (the siblings ARE the
    expansion; the directly-overlapping row alone expands to nothing).
    RLS scopes rows to system + own tenant — call under
    ``tenant_connection``."""
    if not lexemes:
        return []
    return await conn.fetch(
        "SELECT group_id, term, lexemes FROM medical_synonyms "
        "WHERE group_id IN ("
        "  SELECT group_id FROM medical_synonyms WHERE lexemes && $1::text[]"
        ")",
        lexemes,
    )


def assemble_tsquery(
    query_lexemes: list[str], synonym_rows: list, *, max_expansions: int = MAX_EXPANSIONS
) -> ExpandedQuery:
    """Pure assembly — unit-testable without a DB.

    ``synonym_rows``: (group_id, term, lexemes) mappings. For each query
    lexeme, alternatives = every term in every group that CONTAINS that
    lexeme, minus atoms equal to the lexeme itself.
    """
    if not query_lexemes:
        return ExpandedQuery(tsquery=None)

    by_group: dict[UUID, list[tuple[str, list[str]]]] = {}
    for row in synonym_rows:
        by_group.setdefault(row["group_id"], []).append(
            (row["term"], list(row["lexemes"]))
        )

    conjuncts: list[str] = []
    expanded_terms: list[str] = []
    groups_fired: set[UUID] = set()
    expansions_used = 0

    for lexeme in query_lexemes:
        atom = _quote(lexeme)
        if len(lexeme) < MIN_LEXEME_LEN or expansions_used >= max_expansions:
            conjuncts.append(atom)
            continue
        alternatives: list[str] = []
        for group_id, terms in by_group.items():
            if not any(lexeme in lexes for _, lexes in terms):
                continue
            fired = False
            for _term, lexes in terms:
                alt = _term_atom(lexes)
                if alt != atom and alt not in alternatives:
                    alternatives.append(alt)
                    fired = True
            if fired:
                groups_fired.add(group_id)
                for term, lexes in terms:
                    if lexes != [lexeme] and term not in expanded_terms:
                        expanded_terms.append(term)
        if alternatives:
            expansions_used += 1
            conjuncts.append("(" + " | ".join([atom, *alternatives]) + ")")
        else:
            conjuncts.append(atom)

    return ExpandedQuery(
        tsquery=" & ".join(conjuncts),
        expanded_terms=expanded_terms,
        groups_used=len(groups_fired),
    )


async def expand_query(conn: asyncpg.Connection, *, raw_q: str) -> ExpandedQuery:
    """The full expansion: normalize → lookup → assemble."""
    lexemes = await normalize_lexemes(conn, raw_q)
    if not lexemes:
        return ExpandedQuery(tsquery=None)
    rows = await fetch_matching_synonyms(conn, lexemes=lexemes)
    return assemble_tsquery(lexemes, rows)
