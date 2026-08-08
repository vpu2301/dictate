"""S02 verification §9.5 of the sprint spec: 100% probe quarantine + zero
false positives on benign clinical lookalikes. Probe fixture is shared with
the shared probe set (libs/evidence_safety/tests/fixtures/injection_probes.json).
The same probes run against ingested corpus documents and fetched web pages —
one screen, one fixture set (EVA-S04 moved both out of evidence-ingest)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evidence_safety import screen_text

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "injection_probes.json"


def _fixture() -> dict[str, list[str]]:
    return json.loads(FIXTURE.read_text())


def test_all_probes_detected() -> None:
    probes = _fixture()["probes"]
    assert len(probes) >= 12
    missed = [p for p in probes if not screen_text(p)]
    assert not missed, f"undetected injection probes: {missed}"


def test_probes_detected_inside_document_context() -> None:
    probe = _fixture()["probes"][0]
    document = "1. Diagnosis\n\nOrdinary clinical text here.\n\n" + probe + "\n\nMore text."
    assert screen_text(document)


def test_benign_lookalikes_pass() -> None:
    benign = _fixture()["benign"]
    assert len(benign) >= 6
    false_positives = {b: [h.pattern for h in screen_text(b)] for b in benign if screen_text(b)}
    assert not false_positives, f"false positives: {false_positives}"


@pytest.mark.parametrize(
    "clinical_text",
    [
        "The patient was instructed to ignore transient side effects during the first week.",
        "Попередні інструкції щодо дозування залишаються чинними.",
        "System review: cardiovascular — normal. Previous findings unchanged.",
        "Інструкція із застосування препарату додається до упаковки.",
    ],
)
def test_ordinary_clinical_prose_never_triggers(clinical_text: str) -> None:
    assert screen_text(clinical_text) == []
