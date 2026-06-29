"""Aggregation + output tests for run_wer's corpus mode.

Builds a RunResult directly (no GPU / Whisper) and checks the
word-weighted overall WER, the per-specialty mean's single-word
exclusion, RTF percentiles, number-norm aggregation, the JSON report
shape, and the Prometheus metric names.
"""

from __future__ import annotations

from pathlib import Path

import run_wer


def _utt(**kw):
    base = {
        "utterance_id": "x",
        "language": "uk",
        "specialty": "cardiology",
        "duration_s": 5.0,
        "wer": 0.0,
        "n_ref_words": 10,
        "cer": 0.0,
        "rtf": 6.0,
        "number_norm_score": 1.0,
        "number_norm_by_category": {"bp": 1.0},
        "reference": "ref",
        "hypothesis": "hyp",
    }
    base.update(kw)
    return run_wer.UtteranceScore(**base)


def _run(utts):
    return run_wer.RunResult(
        run_id="00000000-0000-0000-0000-000000000001",
        started_at="2026-05-10T03:00:00+00:00",
        finished_at="2026-05-10T03:05:00+00:00",
        corpus_version="v1",
        model="large-v3",
        pipeline_version="nlp-v1.0.0",
        prompts_hash="deadbeef" * 8,
        utterances=utts,
    )


def test_overall_wer_is_word_weighted():
    # 0.1 over 10 words and 0.5 over 30 words → weighted = (1 + 15)/40 = 0.4
    run = _run(
        [
            _utt(wer=0.1, n_ref_words=10),
            _utt(wer=0.5, n_ref_words=30),
        ]
    )
    assert run.wer_overall("uk") == 0.4
    assert run.wer_overall("en") is None  # no EN utterances


def test_specialty_mean_excludes_single_word_refs():
    run = _run(
        [
            _utt(wer=0.2, n_ref_words=10, specialty="cardiology"),
            _utt(wer=1.0, n_ref_words=1, specialty="cardiology"),  # excluded
        ]
    )
    # Only the 10-word utterance counts for the per-specialty mean.
    assert run.specialty_wer()[("uk", "cardiology")] == 0.2
    # But the single-word utterance still counts toward the overall mean.
    assert run.wer_overall("uk") == run._word_weighted_wer("uk")
    assert run.wer_overall("uk") > 0.2


def test_rtf_percentiles():
    run = _run([_utt(rtf=4.0), _utt(rtf=6.0), _utt(rtf=8.0)])
    assert run.rtf_percentile(50) == 6.0


def test_number_norm_aggregate():
    run = _run(
        [
            _utt(number_norm_by_category={"bp": 1.0, "dose": 1.0}),
            _utt(number_norm_by_category={"bp": 0.0}),
        ]
    )
    agg = run.number_norm_by_category()
    assert agg["bp"] == 0.5
    assert agg["dose"] == 1.0


def test_report_dict_shape():
    run = _run([_utt(number_norm_score=None, number_norm_by_category={})])
    report = run_wer._report_dict(run)
    assert report["run_id"] == run.run_id
    assert report["model"] == "large-v3"
    assert report["pipeline_version"] == "nlp-v1.0.0"
    assert report["utterances"][0]["number_norm_score"] is None
    assert "wer_overall_uk" in report["aggregates"]


def test_emit_prom_metric_names(tmp_path: Path):
    run = _run(
        [
            _utt(language="uk", wer=0.12, cer=0.05, rtf=6.0, n_ref_words=10),
            _utt(language="en", wer=0.08, cer=0.03, rtf=7.0, n_ref_words=10),
        ]
    )
    out = tmp_path / "mdx_wer.prom"
    run_wer._emit_prom(run, out)
    text = out.read_text("utf-8")
    assert 'mdx_wer_overall{language="uk"}' in text
    assert 'mdx_wer_specialty{language="uk",specialty="cardiology"}' in text
    assert 'mdx_cer_overall{language="en"}' in text
    assert "mdx_rtf_p95 " in text
    assert 'mdx_number_norm_accuracy{category="bp"}' in text
