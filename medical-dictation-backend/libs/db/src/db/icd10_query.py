"""Sprint-13: the single ICD-10 (МКХ-10) ranking query.

Two consumers must rank identically or the picker and the extractor
would disagree about what "the best match" is:

- report-service ``GET /v1/icd10/search`` (the FE picker's typing path)
- nlp-service ``stages/icd10_repository`` (step-05 extractor proposals)

Both import :data:`ICD10_SEARCH_SQL` from here — there is no second
copy to drift. ``icd10_codes`` is a global reference table with no
tenant dimension (no RLS), so a plain pool connection is correct;
neither consumer needs ``tenant_connection``.

Ranking, in order:
  0. exact code match (case-insensitive)
  1. code prefix match ("I1" → I10, I10.0 …)
  2. full-text match over displays + code

Within a tier: leaves before headings (only leaves are selectable in
the picker; headings render as group context), then ``ts_rank`` desc,
then code asc as a deterministic tiebreak. Deterministic ordering
matters for the nlp pipeline's byte-equal replay contract.

FTS uses the ``simple`` config per ADR-0021 — no Ukrainian stemmer.
"""

from __future__ import annotations

from typing import Final

# $1 = raw query text, $2 = limit.
#
# The FTS tier matches by PREFIX on every lexeme (``'гіперт':*``), not
# whole words: this query sits in the picker's search-as-you-type path,
# where almost every keystroke is a partial word — ``plainto_tsquery``'s
# whole-word matching made mid-typing queries return nothing (found live
# 2026-07-23: «гіперт» → 0 rows while «гіпертензія» matched). Lexemes
# are normalized through ``to_tsvector('simple', …)`` and re-quoted via
# ``quote_literal`` (apostrophes in words like «м'язів» would otherwise
# break the tsquery syntax). A complete word still matches itself, so
# the extractor's full-word behavior is unchanged — only broadened to
# derivatives sharing the prefix.
#
# Punctuation-only input produces zero lexemes → ``ts_q`` is NULL → the
# ``@@`` tier is skipped, which is why the exact/prefix tiers are
# separate OR-ed predicates rather than a filter on the FTS match.
ICD10_SEARCH_SQL: Final = """
WITH q AS (
    SELECT
        upper(btrim($1)) AS code_q,
        (
            SELECT to_tsquery('simple', string_agg(quote_literal(lex) || ':*', ' & '))
            FROM unnest(tsvector_to_array(to_tsvector('simple', btrim($1)))) AS t(lex)
        ) AS ts_q
)
SELECT
    c.code,
    c.display_uk,
    c.display_en,
    c.is_leaf,
    CASE
        WHEN c.code = q.code_q THEN 0
        WHEN c.code LIKE q.code_q || '%' THEN 1
        ELSE 2
    END AS tier,
    ts_rank(c.search_vector, q.ts_q) AS rank
FROM icd10_codes c, q
WHERE c.code = q.code_q
   OR c.code LIKE q.code_q || '%'
   OR (q.ts_q IS NOT NULL AND c.search_vector @@ q.ts_q)
ORDER BY tier ASC, c.is_leaf DESC, rank DESC, c.code ASC
LIMIT $2
"""
