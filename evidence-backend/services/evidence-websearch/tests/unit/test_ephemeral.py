"""Ephemeral index: chunking offsets, overlap, caps, and cache-key hygiene."""

from __future__ import annotations

from evidence_websearch.domain.cache import normalize_url, url_hash
from evidence_websearch.domain.ephemeral import EphemeralIndex, chunk_text

TEXT = (
    "Empiric therapy should be started promptly. Severity assessment guides "
    "the choice of agent. Amoxicillin is first line for mild disease. "
    "Macrolides are added when atypical pathogens are suspected. Duration is "
    "five days in most responders. Review at 48 to 72 hours is recommended. "
    "De-escalate once culture results are available. Consider comorbidity."
)


def _chunks(size: int = 120, overlap: int = 20, max_chunks: int = 40) -> list:
    return chunk_text(
        TEXT,
        page_url="https://who.int/cap",
        domain="who.int",
        title="CAP",
        size=size,
        overlap=overlap,
        max_chunks=max_chunks,
    )


def test_chunks_cover_the_text_and_carry_resolvable_offsets() -> None:
    chunks = _chunks()

    assert chunks
    for chunk in chunks:
        assert 0 <= chunk.char_start < chunk.char_end <= len(TEXT)
        # The offsets must point into the stored extract, which is what makes
        # a citation resolvable on reopen without re-fetching.
        window = TEXT[chunk.char_start : chunk.char_end]
        first_words = chunk.text.split()[:3]
        assert " ".join(first_words) in " ".join(window.split()) or chunk.char_start == 0


def test_chunk_ids_are_stable_across_runs() -> None:
    """Deterministic ids: the same page chunked twice must cite identically."""
    assert [c.id for c in _chunks()] == [c.id for c in _chunks()]


def test_chunking_respects_the_max_chunk_cap() -> None:
    chunks = _chunks(size=20, overlap=5, max_chunks=3)
    assert len(chunks) == 3


def test_empty_text_produces_no_chunks() -> None:
    assert (
        chunk_text("   ", page_url="u", domain="d", title="t", size=100, overlap=10, max_chunks=5)
        == []
    )


def test_chunks_carry_page_provenance() -> None:
    chunk = _chunks()[0]
    assert chunk.page_url == "https://who.int/cap"
    assert chunk.domain == "who.int"
    assert chunk.id.startswith("web:")


def test_index_key_is_per_tenant() -> None:
    """Two tenants asking the identical question must not share an index —
    the cached passages are tenant data."""
    a = EphemeralIndex.key("11111111-1111-1111-1111-111111111111", "pneumonia therapy")
    b = EphemeralIndex.key("22222222-2222-2222-2222-222222222222", "pneumonia therapy")
    assert a != b


def test_index_key_is_stable_for_the_same_query() -> None:
    tenant = "11111111-1111-1111-1111-111111111111"
    assert EphemeralIndex.key(tenant, "q") == EphemeralIndex.key(tenant, "q")


def test_url_normalization_collapses_only_the_irrelevant_differences() -> None:
    base = "https://WHO.int/publications/item?id=7#section-2"

    assert normalize_url(base) == "https://who.int/publications/item?id=7"
    # Query preserved: on many guideline sites it selects the document.
    assert normalize_url("https://who.int/x?a=1") != normalize_url("https://who.int/x?a=2")
    # Fragment ignored: it is a position in one page, not a different page.
    assert url_hash("https://who.int/x#a") == url_hash("https://who.int/x#b")
    # Empty path normalizes to root.
    assert url_hash("https://who.int") == url_hash("https://who.int/")
