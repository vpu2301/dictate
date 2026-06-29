"""Unit tests for the sprint-07 WER scoring + corpus-integrity contract.

These protect the measurement contract (docs/eval/wer-methodology.md):
the UK-aware tokeniser, Levenshtein WER/CER, per-category number-norm,
determinism, and manifest SHA-256 integrity.
"""

from __future__ import annotations

import json
import wave
from pathlib import Path

import pytest
import wer_lib


# ── Tokenisation / WER ────────────────────────────────────────────────
def test_tokenize_keeps_cyrillic_and_apostrophe():
    assert wer_lib.tokenize("П'ять Мг, 120/80!") == ["п'ять", "мг", "120", "80"]


def test_wer_counts_ukrainian_wrong_case_as_error():
    # "інфаркту" (genitive) vs "інфаркт" (nominative) — a real case error
    # that English-style stemming would mask. Must count as 1 substitution.
    ref = "гострий інфаркт міокарда"
    hyp = "гострий інфаркту міокарда"
    rate, n_ref = wer_lib.wer(ref, hyp)
    assert n_ref == 3
    assert rate == pytest.approx(1 / 3)


def test_wer_perfect_is_zero():
    assert wer_lib.wer("blood pressure normal", "blood pressure normal") == (0.0, 3)


def test_wer_empty_reference_edge_cases():
    assert wer_lib.wer("", "") == (0.0, 0)
    assert wer_lib.wer("", "spurious words") == (1.0, 0)


def test_cer_case_insensitive_single_sub():
    rate, n = wer_lib.cer("abcd", "abxd")
    assert n == 4
    assert rate == pytest.approx(0.25)
    assert wer_lib.cer("ABCD", "abcd")[0] == 0.0


# ── Number-norm ───────────────────────────────────────────────────────
def test_number_norm_bp_and_dose_perfect():
    ref = "АТ 120/80 мм рт ст, бісопролол 5 мг"
    scores = wer_lib.number_norm_by_category(ref, ref)
    assert scores["bp"] == 1.0
    assert scores["dose"] == 1.0


def test_number_norm_missing_bp_scores_zero():
    ref = "АТ 120/80, бісопролол 5 мг"
    hyp = "бісопролол 5 мг"  # dropped the blood pressure
    scores = wer_lib.number_norm_by_category(ref, hyp)
    assert scores["bp"] == 0.0
    assert scores["dose"] == 1.0


def test_number_norm_overall_none_when_no_numbers():
    assert wer_lib.number_norm_overall("no numbers here", "still none") is None


def test_number_norm_overall_combined_ratio():
    ref = "АТ 120/80, доза 5 мг"  # 1 bp + 1 dose = 2 instances
    hyp = "АТ 120/80, доза 9 мг"  # bp matches, dose wrong → 1/2
    assert wer_lib.number_norm_overall(ref, hyp) == pytest.approx(0.5)


# ── Percentiles ───────────────────────────────────────────────────────
def test_percentile():
    vals = [1.0, 2.0, 3.0, 4.0]
    assert wer_lib.percentile(vals, 50) == pytest.approx(2.5)
    assert wer_lib.percentile([5.0], 95) == 5.0
    assert wer_lib.percentile([], 95) == 0.0


# ── Determinism ───────────────────────────────────────────────────────
def test_determinism_same_input_same_score():
    ref, hyp = "пацієнт стабільний 120/80", "пацієнт стабільний 120 80"
    assert wer_lib.wer(ref, hyp) == wer_lib.wer(ref, hyp)
    assert wer_lib.cer(ref, hyp) == wer_lib.cer(ref, hyp)
    assert wer_lib.number_norm_by_category(ref, hyp) == wer_lib.number_norm_by_category(
        ref, hyp
    )


# ── Manifest integrity ────────────────────────────────────────────────
def _make_utterance(d: Path, uid: str) -> None:
    d.mkdir(parents=True)
    with wave.open(str(d / "audio.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 1600)
    (d / "transcript.txt").write_text("тест\n", encoding="utf-8")
    (d / "metadata.json").write_text(
        json.dumps(
            {
                "utterance_id": uid,
                "language": "uk",
                "specialty": "general",
                "duration_s": 0.1,
                "dictation_source": "authored_by_linguist",
            }
        ),
        encoding="utf-8",
    )


def test_manifest_build_then_verify_clean(tmp_path: Path):
    _make_utterance(tmp_path / "uk-general-001", "uk-general-001")
    manifest = wer_lib.build_manifest(tmp_path)
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert len(manifest["utterances"]) == 1
    assert wer_lib.verify_manifest(tmp_path) == []


def test_manifest_detects_tampered_audio(tmp_path: Path):
    _make_utterance(tmp_path / "uk-general-001", "uk-general-001")
    manifest = wer_lib.build_manifest(tmp_path)
    # Tamper with the .wav AFTER the manifest is built.
    with wave.open(str(tmp_path / "uk-general-001" / "audio.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x01\x02" * 3200)
    problems = wer_lib.verify_manifest(tmp_path, manifest)
    assert problems
    assert any("audio.wav" in p and "mismatch" in p for p in problems)


def test_manifest_detects_missing_dir(tmp_path: Path):
    _make_utterance(tmp_path / "uk-general-001", "uk-general-001")
    manifest = wer_lib.build_manifest(tmp_path)
    import shutil

    shutil.rmtree(tmp_path / "uk-general-001")
    problems = wer_lib.verify_manifest(tmp_path, manifest)
    assert any("missing" in p for p in problems)
