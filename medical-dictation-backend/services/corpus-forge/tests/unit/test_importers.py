"""Importers — including the known-mojibake Windows-1251 fixture (plan §3)."""

from corpus_forge.domain.importers import dataset_source_ref, parse_drlz_csv, parse_term_lines

# «Бісопролол;таблетки 5 мг» in genuine Windows-1251 bytes. Decoding this
# as UTF-8/latin-1 yields mojibake («Á³ñîïðîëîë…») — the exact failure the
# plan's encoding note warns about.
DRLZ_W1251 = "Торговельна назва;Форма випуску\nБісопролол;таблетки 5 мг\nАторвастатин;\n".encode(
    "windows-1251"
)


def test_drlz_decodes_windows_1251_not_mojibake() -> None:
    terms = parse_drlz_csv(DRLZ_W1251)
    texts = [t.text for t in terms]
    assert "Бісопролол" in texts
    assert "Бісопролол таблетки 5 мг" in texts
    assert "Аторвастатин" in texts
    assert all("Á" not in t and "³" not in t for t in texts), "mojibake leaked through"


def test_drlz_wrong_decode_produces_mojibake() -> None:
    """Documents WHY the explicit decode exists: latin-1 turns the same bytes
    into garbage that would sail through a naive importer."""
    garbled = DRLZ_W1251.decode("latin-1")
    assert "Бісопролол" not in garbled


def test_drlz_all_terms_map_to_plan_section() -> None:
    assert {t.section for t in parse_drlz_csv(DRLZ_W1251)} == {"plan"}


def test_drlz_deduplicates_case_insensitively() -> None:
    raw = "Назва;Форма\nАспірин;\nаспірин;\n".encode("windows-1251")
    assert len(parse_drlz_csv(raw)) == 1


def test_term_lines_strips_comments_and_dedupes() -> None:
    raw = "Гіпертонічна хвороба  # essential HTN\n\nГіпертонічна хвороба\nЦукровий діабет\n".encode()
    terms = parse_term_lines(raw, section="diagnosis")
    assert [t.text for t in terms] == ["Гіпертонічна хвороба", "Цукровий діабет"]


def test_source_ref_pins_dataset_version_and_sha() -> None:
    ref = dataset_source_ref("drlz", "2026-08-09", b"payload")
    dataset, version, sha = ref.split(":")
    assert (dataset, version) == ("drlz", "2026-08-09")
    assert len(sha) == 64
