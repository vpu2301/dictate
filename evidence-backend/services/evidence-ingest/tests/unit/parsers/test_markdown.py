from __future__ import annotations

from pathlib import Path

from evidence_ingest.domain.parsers import markdown


def test_protocol_md(fixtures_dir: Path) -> None:
    doc = markdown.parse((fixtures_dir / "protocol.md").read_bytes())
    assert doc.title == "Протокол лікування артеріальної гіпертензії"
    assert doc.language == "uk"
    assert doc.metadata["doi"] == "10.3333/moz.2022.15"
    assert doc.metadata["published"] == "2022-05-12"
    assert doc.metadata["moz_order"] == "384"
    assert [(s.path, s.level) for s in doc.sections] == [
        ("Загальні положення", 2),
        ("Лікування", 2),
        ("Лікування > Контроль тиску", 3),
    ]
    table_section = doc.sections[1]
    assert "Препарат\tДоза" in table_section.text
    assert "Амлодипін\t5 мг" in table_section.text
    assert "| ---" not in table_section.text
    fenced = doc.sections[2]
    assert "```" in fenced.text
    assert "АТ = 120/80 мм рт. ст." in fenced.text


def test_title_falls_back_to_first_h1() -> None:
    doc = markdown.parse(b"# Top heading\n\nSome body text.\n")
    assert doc.title == "Top heading"
    assert [(s.path, s.level) for s in doc.sections] == [("Top heading", 1)]
