from __future__ import annotations

from pathlib import Path

import pytest
from evidence_ingest.domain.parse_types import ParserError
from evidence_ingest.domain.parsers import docx


def test_clinical_note(fixtures_dir: Path) -> None:
    doc = docx.parse((fixtures_dir / "clinical_note.docx").read_bytes())
    assert doc.title == "Клінічна настанова з ведення пацієнтів"
    assert doc.language == "uk"
    assert doc.metadata["moz_order"] == "1234"
    assert doc.metadata["published"] == "2023-11-02"
    assert [(s.path, s.level) for s in doc.sections] == [
        ("Загальні відомості", 1),
        ("Загальні відомості > Обстеження", 2),
        ("Лікування", 1),
    ]
    exam = doc.sections[1]
    assert "Показник\tЗначення" in exam.text
    assert "Гемоглобін\t120 г/л" in exam.text


def test_invalid_docx_raises() -> None:
    with pytest.raises(ParserError, match="invalid_docx"):
        docx.parse(b"PK\x03\x04 truncated and not a real zip archive")
