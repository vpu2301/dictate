"""CSV import parsing and validation (corpus-v2 §6).

The first test is the acceptance criterion verbatim: the shipped
``eval/corpus/v2/corpus-v2-replicas.csv`` must import "без ручних правок" —
86 rows, zero rejections, and the coverage matrix §3 promises.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from autocomplete_service import eval_import as ei

CSV_PATH = (
    Path(__file__).resolve().parents[4] / "eval" / "corpus" / "v2" / "corpus-v2-replicas.csv"
)

HEADER = "id,lang,category,condition,set,text\n"


def _csv(*rows: str) -> str:
    return HEADER + "".join(r + "\n" for r in rows)


def test_the_shipped_v2_file_imports_with_no_manual_edits():
    parsed = ei.parse(ei.decode(CSV_PATH.read_bytes()))
    assert parsed.rejected == []
    assert len(parsed.rows) == 86
    assert parsed.warnings == []


def test_the_shipped_file_produces_the_coverage_matrix_section_3_promises():
    parsed = ei.parse(ei.decode(CSV_PATH.read_bytes()))
    coverage = {
        (c["language"], c["subset"]): c["utterances"]
        for c in ei.coverage_matrix([], parsed.rows)
    }
    assert coverage[("uk", "numbers_doses_units")] == 8
    assert coverage[("uk", "drug_names")] == 6
    assert coverage[("en", "baseline")] == 10
    assert coverage[("en", "numbers_doses_units")] == 12
    assert sum(coverage.values()) == 86


def test_every_row_lands_in_dev_and_the_conditions_map_to_the_schema():
    parsed = ei.parse(ei.decode(CSV_PATH.read_bytes()))
    assert {r.dataset for r in parsed.rows} == {"dev"}
    # §6 offers two condition names; the schema has four. phone_noise is the
    # harsher of the two, and the sprint-18 dependency.
    assert {r.condition for r in parsed.rows} == {"headset", "phone-speaker-distance"}


def test_the_bom_excel_insists_on_is_accepted():
    body = _csv("uk-num-001,uk,numbers,headset,dev,\"Тиск сто сорок.\"")
    assert ei.decode(b"\xef\xbb\xbf" + body.encode()) == ei.decode(body.encode())


def test_a_semicolon_file_is_refused_as_a_whole_rather_than_row_by_row():
    """Excel's regional default. 86 identical "bad row" verdicts would bury
    the one fact that matters."""
    with pytest.raises(ei.CsvFormatError) as exc:
        ei.parse("id;lang;category;condition;set;text\nuk-a;uk;base;headset;dev;т\n")
    assert exc.value.code == "bad_delimiter"


def test_missing_columns_name_themselves():
    with pytest.raises(ei.CsvFormatError) as exc:
        ei.parse("id,lang,text\nuk-a,uk,т\n")
    assert exc.value.code == "missing_columns"
    assert "category" in exc.value.detail


def test_test_set_is_refused_without_an_explicit_confirmation():
    """The holdout guard (§1.2). Not a malformed file — a refusal to write
    into the frozen set without being told to."""
    row = "uk-base-001,uk,base,headset,test,\"Пацієнт почувається краще.\""
    refused = ei.parse(_csv(row))
    assert refused.rows == []
    assert refused.rejected[0].code == "test_requires_confirmation"

    allowed = ei.parse(_csv(row), allow_test=True)
    assert allowed.rejected == []
    assert allowed.rows[0].dataset == "test"


@pytest.mark.parametrize(
    ("row", "code", "field"),
    [
        ("uk-a-1,zz,base,headset,dev,текст", "unknown_language", "lang"),
        ("uk-a-1,uk,poetry,headset,dev,текст", "unknown_category", "category"),
        ("uk-a-1,uk,base,megaphone,dev,текст", "unknown_condition", "condition"),
        ("uk-a-1,uk,base,headset,prod,текст", "unknown_set", "set"),
        ("UK-A-1,uk,base,headset,dev,текст", "bad_id", "id"),
        # The id template's language half is enforced: it is the part that
        # silently produces a corpus scored with the wrong hint.
        ("en-a-1,uk,base,headset,dev,текст", "id_language_mismatch", "id"),
        ("uk-a-1,uk,base,headset,dev,", "bad_text", "text"),
    ],
)
def test_bad_rows_are_rejected_individually_with_a_reason(row, code, field):
    parsed = ei.parse(_csv(row))
    assert parsed.rows == []
    assert (parsed.rejected[0].code, parsed.rejected[0].field) == (code, field)


def test_one_bad_row_does_not_cost_the_others_their_verdict():
    """A file is a batch of work; "row 2 is broken" is only actionable next
    to the other verdicts."""
    parsed = ei.parse(
        _csv(
            "uk-a-1,uk,base,headset,dev,перший",
            "uk-a-2,uk,poetry,headset,dev,другий",
            "uk-a-3,uk,base,headset,dev,третій",
        )
    )
    assert [r.script_id for r in parsed.rows] == ["uk-a-1", "uk-a-3"]
    assert parsed.rejected[0].line_no == 3


def test_text_over_two_hundred_characters_is_refused():
    parsed = ei.parse(_csv(f"uk-a-1,uk,base,headset,dev,{'а' * 201}"))
    assert parsed.rejected[0].code == "bad_text"


def test_a_repeated_id_inside_one_file_is_rejected_not_silently_deduplicated():
    parsed = ei.parse(
        _csv(
            "uk-a-1,uk,base,headset,dev,перший",
            "uk-a-1,uk,base,headset,dev,другий",
        )
    )
    assert len(parsed.rows) == 1
    assert parsed.rejected[0].code == "duplicate_id_in_file"


def test_a_repeated_text_is_a_warning_not_a_refusal():
    """Two conditions recording the same sentence is a legitimate design
    (§1.2); a copy-paste that was meant to be edited is not. Only the author
    can tell them apart, so the import says so and continues."""
    parsed = ei.parse(
        _csv(
            "uk-a-1,uk,base,headset,dev,однаковий",
            "uk-a-2,uk,base,phone_noise,dev,однаковий",
        )
    )
    assert len(parsed.rows) == 2
    assert parsed.warnings[0]["code"] == "duplicate_text"
    assert parsed.warnings[0]["same_as"] == "uk-a-1"


def test_pii_never_becomes_a_row_whichever_door_it_arrives_through():
    parsed = ei.parse(
        _csv("uk-a-1,uk,base,headset,dev,\"Пацієнт ІПН 1234567890 скаржиться\"")
    )
    assert parsed.rows == []
    assert parsed.rejected[0].code == "pii_detected"


def test_optional_columns_default_without_being_required():
    parsed = ei.parse(_csv("uk-a-1,uk,numbers,headset,dev,\"Тиск сто сорок\""))
    row = parsed.rows[0]
    assert row.specialty == ei.DEFAULT_SPECIALTY
    # Gold text defaults to the spoken form — the honest default when a line
    # has no normalisation to test.
    assert row.transcript == row.say


def test_an_explicit_gold_text_column_is_honoured():
    parsed = ei.parse(
        "id,lang,category,condition,set,text,transcript\n"
        "uk-a-1,uk,numbers,headset,dev,\"Тиск сто сорок на дев'яносто\",\"Тиск 140/90\"\n"
    )
    assert parsed.rows[0].transcript == "Тиск 140/90"


def test_categories_map_onto_the_corpus_subsets():
    parsed = ei.parse(
        _csv(
            "uk-a-1,uk,base,headset,dev,а",
            "uk-a-2,uk,numbers,headset,dev,б",
            "uk-a-3,uk,commands,headset,dev,в",
        )
    )
    assert [r.subset for r in parsed.rows] == [
        None,
        "numbers_doses_units",
        "voice_commands",
    ]


def test_pending_rows_are_folded_into_the_existing_coverage():
    """The dry run shows the coverage the COMMIT would produce, which is the
    number §1.2's design table is read against."""
    existing = [
        {"dataset": "test", "language": "uk", "subset": "numbers_doses_units",
         "utterances": 5}
    ]
    parsed = ei.parse(_csv("uk-a-1,uk,numbers,headset,dev,а"))
    matrix = {
        (c["dataset"], c["subset"]): c["utterances"]
        for c in ei.coverage_matrix(existing, parsed.rows)
    }
    assert matrix[("test", "numbers_doses_units")] == 5
    assert matrix[("dev", "numbers_doses_units")] == 1


def test_an_empty_file_is_a_format_error_not_an_empty_success():
    with pytest.raises(ei.CsvFormatError) as exc:
        ei.parse("   ")
    assert exc.value.code == "empty_file"
