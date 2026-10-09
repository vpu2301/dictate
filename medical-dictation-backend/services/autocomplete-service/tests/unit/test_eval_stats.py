"""Intervals, weighting and paired comparison (corpus-v2 §4 P0-2, P1-5, P1-7).

Two properties carry the rest: the numbers must not move when nothing
changed (determinism, §4 P0-4), and a comparison must be able to say "this
difference is indistinguishable from resampling noise" — otherwise every
run looks like progress.
"""

from __future__ import annotations

import pytest
from autocomplete_service import eval_stats


def _items(wers: list[float], **over) -> list[dict]:
    base = []
    for i, w in enumerate(wers):
        item = {
            "script_id": f"u{i:03d}",
            "wer": w,
            "ref_words": 10,
            "cer": w / 2,
            "ref_chars": 60,
            "wer_norm": max(0.0, w - 0.05),
            "ref_words_norm": 10,
            "cer_norm": w / 2,
            "ref_chars_norm": 60,
            "subset": None,
            "language": "uk",
            "condition": "headset",
            "dose_tokens": 0,
            "dose_exact": None,
        }
        item.update(over)
        base.append(item)
    return base


def test_summary_is_identical_whatever_order_the_rows_arrive_in():
    """The bootstrap draws from a list, and a list that came out of a
    database has no guaranteed order. Sorting by script_id first is what
    makes "two runs on identical data give identical numbers" true of the
    intervals and not just the point estimates."""
    items = _items([0.1, 0.4, 0.25, 0.3, 0.15, 0.5, 0.2, 0.35, 0.22, 0.28])
    assert eval_stats.summarize(items) == eval_stats.summarize(list(reversed(items)))


def test_the_same_seed_gives_the_same_interval_twice():
    items = _items([0.1, 0.4, 0.25, 0.3, 0.15])
    first = eval_stats.bootstrap_ci(items, metric="wer", weight="ref_words")
    second = eval_stats.bootstrap_ci(items, metric="wer", weight="ref_words")
    assert first == second


def test_interval_brackets_the_point_estimate():
    items = _items([0.1, 0.4, 0.25, 0.3, 0.15, 0.5, 0.2, 0.35, 0.22, 0.28])
    summary = eval_stats.summarize(items)
    low, high = summary["overall"]["wer_ci"]
    assert low <= summary["overall"]["wer"] <= high


def test_small_buckets_are_flagged_rather_than_hidden():
    """§1.1's whole complaint: a category WER on n=5 is quoted as though it
    were a result. The number stays visible; the flag is what stops it from
    being read as one."""
    assert eval_stats.summarize(_items([0.2] * 5))["overall"]["insufficient_data"]
    assert not eval_stats.summarize(_items([0.2] * 10))["overall"]["insufficient_data"]


def test_wer_is_weighted_by_reference_words_not_averaged_per_utterance():
    items = [
        {"script_id": "short", "wer": 1.0, "ref_words": 3, "ref_chars": 10,
         "wer_norm": 1.0, "ref_words_norm": 3},
        {"script_id": "long", "wer": 0.0, "ref_words": 30, "ref_chars": 100,
         "wer_norm": 0.0, "ref_words_norm": 30},
    ]
    assert eval_stats.summarize(items)["overall"]["wer"] == pytest.approx(
        3 / 33, abs=1e-4
    )


def test_dose_accuracy_ignores_utterances_with_no_numbers():
    """A corpus of clean prose would otherwise report 100% dose accuracy and
    mean nothing by it."""
    items = _items([0.2] * 4)
    items[0].update(dose_tokens=2, dose_exact=True)
    items[1].update(dose_tokens=3, dose_exact=False)
    dose = eval_stats.summarize(items)["overall"]["dose"]
    assert dose == {"utterances": 2, "exact": 1, "accuracy": 0.5}


def test_dose_accuracy_is_null_when_nothing_numeric_was_measured():
    assert eval_stats.summarize(_items([0.2] * 3))["overall"]["dose"]["accuracy"] is None


def test_buckets_split_by_subset_language_and_condition():
    items = _items([0.2, 0.3]) + _items([0.4], subset="drug_names", condition="noisy")
    summary = eval_stats.summarize(items)
    assert set(summary["by_subset"]) == {"baseline", "drug_names"}
    assert set(summary["by_condition"]) == {"headset", "noisy"}
    # §1.2 wants the phone/noise number readable on its own — the clinic's
    # worst case is the one the corpus has to survive.
    assert summary["by_condition"]["noisy"]["wer"] == pytest.approx(0.4)


def test_flagged_items_are_reported_but_never_averaged():
    counted = _items([0.2, 0.2])
    flagged = [
        {
            "script_id": "u999",
            "wer": 1.75,
            "wer_norm": 1.75,
            "flags": ["wer_over_100"],
            "hypothesis": "Дякую за перегляд!",
        }
    ]
    summary = eval_stats.summarize(counted, flagged=flagged)
    assert summary["overall"]["wer"] == pytest.approx(0.2)
    assert summary["overall"]["utterances"] == 2
    assert summary["flagged"]["count"] == 1
    assert summary["flagged"]["items"][0]["flags"] == ["wer_over_100"]


# ── paired comparison ─────────────────────────────────────────────────


def test_a_run_compared_with_itself_is_not_significant():
    items = _items([0.1, 0.4, 0.25, 0.3, 0.15])
    result = eval_stats.paired_delta(items, items)
    assert result["delta"] == 0.0
    assert result["significant"] is False
    assert result["unchanged"] == 5


def test_a_uniform_improvement_is_significant_and_signed():
    baseline = _items([0.3] * 8)
    candidate = [{**i, "wer_norm": i["wer_norm"] - 0.08} for i in baseline]
    result = eval_stats.paired_delta(baseline, candidate)
    assert result["delta"] == pytest.approx(-0.08)
    assert result["significant"] is True
    assert result["improved"][0]["delta"] < 0
    assert result["regressed"][0]["delta"] < 0  # everything moved the same way


def test_comparison_only_uses_utterances_present_in_both_runs():
    baseline = _items([0.2, 0.3, 0.4])
    candidate = _items([0.2, 0.3])
    result = eval_stats.paired_delta(baseline, candidate)
    assert result["utterances"] == 2


def test_disjoint_runs_compare_to_nothing_rather_than_to_zero():
    a = _items([0.2])
    b = [{**i, "script_id": "other"} for i in _items([0.9])]
    result = eval_stats.paired_delta(a, b)
    assert result["utterances"] == 0
    assert result["delta"] is None
    assert result["significant"] is False


def test_improved_and_regressed_lists_are_capped_at_ten():
    baseline = _items([0.5] * 40)
    candidate = [
        {**item, "wer_norm": item["wer_norm"] - (i + 1) / 100}
        for i, item in enumerate(baseline)
    ]
    result = eval_stats.paired_delta(baseline, candidate)
    assert len(result["improved"]) == 10
    assert len(result["regressed"]) == 10
    # Best improvement first.
    assert result["improved"][0]["delta"] <= result["improved"][-1]["delta"]
