from __future__ import annotations

import pytest
from evidence_ingest.domain.enrich import canonical_id_for, content_checksum, enrich
from evidence_ingest.domain.parse_types import ParsedDocument, ParsedSection
from evidence_ingest.domain.retractions import MalformedFeedError, parse_feed


def _doc(metadata: dict[str, str]) -> ParsedDocument:
    return ParsedDocument(
        title="Guideline",
        sections=[ParsedSection(path="", level=0, text="body")],
        metadata=metadata,
    )


class TestCanonicalId:
    def test_doi_wins_over_pmid(self) -> None:
        doc = _doc({"doi": "10.1234/X.5", "pmid": "999"})
        assert canonical_id_for(doc, b"x") == "doi:10.1234/x.5"

    def test_moz_order(self) -> None:
        assert canonical_id_for(_doc({"moz_order": "1234"}), b"x") == "moz:1234"

    def test_content_hash_fallback(self) -> None:
        cid = canonical_id_for(_doc({}), b"same-bytes")
        assert cid.startswith("sha256:")
        assert cid == canonical_id_for(_doc({}), b"same-bytes")
        assert cid != canonical_id_for(_doc({}), b"other-bytes")


class TestEnrich:
    def test_unknown_license_parks_as_restricted(self) -> None:
        metadata = enrich(_doc({}), b"x", {})
        assert metadata.license_class == "restricted"
        assert metadata.license_known is False

    def test_valid_license_kept(self) -> None:
        metadata = enrich(_doc({}), b"x", {"license_class": "open_license"})
        assert metadata.license_class == "open_license"
        assert metadata.license_known is True

    def test_overrides_win_over_parser(self) -> None:
        doc = _doc({"doi": "10.1/a", "published": "2020-01-01"})
        metadata = enrich(
            doc,
            b"x",
            {
                "canonical_id": "moz:77",
                "published": "2024-06-01",
                "source_authority": "national",
                "evidence_tier": "guideline",
            },
        )
        assert metadata.canonical_id == "moz:77"
        assert metadata.published_at == "2024-06-01"
        assert metadata.source_authority == "national"
        assert metadata.evidence_tier == "guideline"

    def test_invalid_classification_falls_back(self) -> None:
        metadata = enrich(
            _doc({}), b"x", {"source_authority": "wikipedia", "evidence_tier": "vibes"}
        )
        assert metadata.source_authority == "other"
        assert metadata.evidence_tier == "other"

    def test_checksum_shape(self) -> None:
        assert content_checksum(b"abc").startswith("sha256:")


class TestRetractionFeed:
    def test_doi_and_pmid_columns(self) -> None:
        raw = b"Title,OriginalPaperDOI,RetractionPubMedID\nA,10.1/xyz,\nB,,12345\n"
        assert parse_feed(raw) == ["doi:10.1/xyz", "pmid:12345"]

    def test_missing_columns_malformed(self) -> None:
        with pytest.raises(MalformedFeedError):
            parse_feed(b"title,reason\nA,dup\n")

    def test_no_header_malformed(self) -> None:
        with pytest.raises(MalformedFeedError):
            parse_feed(b"")

    def test_empty_rows_skipped(self) -> None:
        raw = b"doi\n\n\n10.5/q\n"
        assert parse_feed(raw) == ["doi:10.5/q"]
