"""Intervals, not point estimates — corpus-v2 §1.1 and §4 P0-2.

"Перемикання мов 36.8%" over five utterances is not a measurement of
anything. The bootstrap CI on that number spans roughly 15–60%, which means
the honest reading is "somewhere between fine and terrible" — and a
subsequent run that reports 28% has told you nothing about whether anything
improved. Point estimates on n=5 invite exactly the decision they cannot
support.

So every number this module produces carries an interval, and every bucket
too small to support one says so instead of quietly looking precise.

THE RESAMPLING UNIT IS THE UTTERANCE, not the word. Words inside one
utterance are not independent draws — a single hallucinated line contributes
twenty correlated word errors — so resampling words would report an interval
several times too narrow. Resampling utterances with replacement is the
standard non-parametric answer and needs no distributional assumption.

DETERMINISM IS A REQUIREMENT, NOT A CONVENIENCE. §4 P0-4 asks that two runs
over identical data produce identical numbers. A seeded ``random.Random``
over a stably-sorted item list makes that true of the intervals as well as
the point estimates, so a re-aggregation never shifts a CI by a tenth of a
point and starts an investigation.

Stdlib only, no I/O.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from typing import Any, Final

#: §1.3.6 asks for 1000 iterations; 1000 is also where the 2.5/97.5
#: percentiles stop moving between seeds at corpus sizes of 30–120.
BOOTSTRAP_ITERATIONS: Final = 1000

#: Below this, the bucket is reported WITH its interval and WITH a flag.
#: Hiding the number would be worse — the flag is what stops it from being
#: quoted as though it were a result (§4 P0-2).
MIN_N_FOR_INTERVAL: Final = 10

#: Fixed unless a run overrides it; stored on the run row either way.
DEFAULT_SEED: Final = 20260814

_CI_LOW: Final = 0.025
_CI_HIGH: Final = 0.975


def _percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile on an already-sorted sequence.

    Deliberately not interpolated: with 1000 bootstrap replicates the
    difference is below the third decimal, and a rank is reproducible across
    Python versions in a way that floating-point interpolation is not.
    """
    if not values:
        raise ValueError("empty")
    idx = int(round(q * (len(values) - 1)))
    return values[min(max(idx, 0), len(values) - 1)]


def _weighted(items: Sequence[dict[str, Any]], metric: str, weight: str) -> float | None:
    """Corpus-level error rate: total errors over total reference units.

    Weighted by reference length rather than averaged over utterances — a
    twenty-word line and a three-word line are not equal evidence. This is
    the same rule ``eval_wer.aggregate`` applies, restated here because the
    bootstrap has to recompute it thousands of times over resampled sets.
    """
    total_w = 0.0
    total_e = 0.0
    for item in items:
        value = item.get(metric)
        w = item.get(weight)
        if value is None or not w:
            continue
        total_w += float(w)
        total_e += float(value) * float(w)
    if total_w == 0:
        return None
    return total_e / total_w


def bootstrap_ci(
    items: Sequence[dict[str, Any]],
    *,
    metric: str,
    weight: str,
    seed: int = DEFAULT_SEED,
    iterations: int = BOOTSTRAP_ITERATIONS,
) -> tuple[float, float] | None:
    """95% CI of the weighted error rate, resampling utterances."""
    usable = [i for i in items if i.get(metric) is not None and i.get(weight)]
    if len(usable) < 2:
        return None
    rng = random.Random(seed)
    n = len(usable)
    replicates: list[float] = []
    for _ in range(iterations):
        sample = [usable[rng.randrange(n)] for _ in range(n)]
        value = _weighted(sample, metric, weight)
        if value is not None:
            replicates.append(value)
    if not replicates:
        return None
    replicates.sort()
    return (
        round(_percentile(replicates, _CI_LOW), 4),
        round(_percentile(replicates, _CI_HIGH), 4),
    )


def _dose(items: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Share of utterances whose every numeric token survived (§1.3.2).

    Computed only over utterances whose REFERENCE contains a number: a
    corpus of clean prose would otherwise score 100% dose accuracy and mean
    nothing by it.
    """
    scoped = [i for i in items if (i.get("dose_tokens") or 0) > 0]
    if not scoped:
        return {"utterances": 0, "exact": 0, "accuracy": None}
    exact = sum(1 for i in scoped if i.get("dose_exact"))
    return {
        "utterances": len(scoped),
        "exact": exact,
        "accuracy": round(exact / len(scoped), 4),
    }


def _roll(items: Sequence[dict[str, Any]], *, seed: int) -> dict[str, Any]:
    """One bucket: both WERs, both CERs, both intervals, dose accuracy."""
    words = sum(int(i.get("ref_words") or 0) for i in items)
    chars = sum(int(i.get("ref_chars") or 0) for i in items)
    words_norm = sum(int(i.get("ref_words_norm") or 0) for i in items)
    chars_norm = sum(int(i.get("ref_chars_norm") or 0) for i in items)
    return {
        "utterances": len(items),
        "ref_words": words,
        "ref_words_norm": words_norm,
        "wer": _round(_weighted(items, "wer", "ref_words")),
        "cer": _round(_weighted(items, "cer", "ref_chars")) if chars else None,
        "wer_norm": _round(_weighted(items, "wer_norm", "ref_words_norm")),
        "cer_norm": (
            _round(_weighted(items, "cer_norm", "ref_chars_norm")) if chars_norm else None
        ),
        "wer_ci": bootstrap_ci(items, metric="wer", weight="ref_words", seed=seed),
        "wer_norm_ci": bootstrap_ci(
            items, metric="wer_norm", weight="ref_words_norm", seed=seed
        ),
        # The reader-facing half of the small-sample problem: a bucket under
        # ten utterances is a hint, not a finding.
        "insufficient_data": len(items) < MIN_N_FOR_INTERVAL,
        "dose": _dose(items),
    }


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def summarize(
    scored: Sequence[dict[str, Any]],
    *,
    flagged: Sequence[dict[str, Any]] = (),
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    """The run summary: overall, per subset, per language, per condition.

    ``scored`` are the utterances that count; ``flagged`` are the ones the
    hallucination detector pulled out (§4 P0-3). Flagged items are reported
    in full — script id, flags, and the score they WOULD have contributed —
    and excluded from every average, because a known Whisper-on-silence
    artefact averaged into a corpus WER is how a recording problem gets
    mistaken for a model problem.

    Sorting by script_id before resampling is what makes the intervals
    reproducible: the bootstrap draws from a list, and a list whose order
    came out of a database is not a stable object.
    """
    items = sorted(scored, key=lambda i: str(i.get("script_id") or ""))

    def bucket(key: str, default: str) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            out.setdefault(str(item.get(key) or default), []).append(item)
        return out

    return {
        "overall": _roll(items, seed=seed),
        "by_subset": {
            k: _roll(v, seed=seed) for k, v in sorted(bucket("subset", "baseline").items())
        },
        "by_language": {
            k: _roll(v, seed=seed) for k, v in sorted(bucket("language", "?").items())
        },
        # §1.2 wants headset and phone/noise reported apart, never mixed:
        # the clinic's worst case is the one the number has to survive.
        #
        # READ THIS TABLE WITH ITS WARNING. Each column holds whichever
        # utterances happened to be recorded that way, and in the v2
        # measurement that produced headset 20.8% against noisy 6.1% — not
        # because headsets are worse but because the headset column carried
        # the numeric and drug-name replicas and the noisy one carried three
        # short baselines. Comparing the columns compares the texts. The
        # honest comparison is `paired_conditions` below.
        "by_condition": {
            k: _roll(v, seed=seed)
            for k, v in sorted(bucket("condition", "unknown").items())
        },
        "by_condition_comparable": False,
        # Epic D: the same texts in both conditions, and nothing else.
        "paired_conditions": paired_conditions(items, seed=seed),
        "flagged": {
            "count": len(flagged),
            "items": [
                {
                    "script_id": f.get("script_id"),
                    "flags": f.get("flags") or [],
                    "wer": _round(f.get("wer")),
                    "wer_norm": _round(f.get("wer_norm")),
                    "hypothesis": f.get("hypothesis"),
                }
                for f in sorted(flagged, key=lambda i: str(i.get("script_id") or ""))
            ],
        },
        "bootstrap": {"iterations": BOOTSTRAP_ITERATIONS, "seed": seed},
    }


def paired_conditions(
    items: Sequence[dict[str, Any]], *, seed: int = DEFAULT_SEED
) -> dict[str, Any]:
    """The honest condition comparison — corpus-v3 Epic D.

    ``by_condition`` compares whichever replicas happened to be recorded in
    each condition, which in the v2 measurement made the harsher conditions
    look better because they carried the easier texts. This table compares a
    condition against another condition ON THE SAME UTTERANCE, and refuses
    to include anything else:

      · only replicas marked `paired` (the design says they are recorded
        twice on purpose);
      · only script ids present in BOTH conditions of the pair — a paired
        replica with one take recorded so far contributes nothing yet, and
        counting it would reintroduce the exact bias the table exists to
        remove.

    The interval comes from the same paired bootstrap ``paired_delta`` uses
    for run-over-run comparison, for the same reason: the two columns are
    measurements of the same utterances, and resampling them independently
    would throw the pairing away and widen the interval for nothing.
    """
    pairs = [i for i in items if i.get("paired")]
    by_condition: dict[str, list[dict[str, Any]]] = {}
    for item in pairs:
        by_condition.setdefault(str(item.get("condition") or "unknown"), []).append(item)

    conditions = sorted(by_condition)
    comparisons: list[dict[str, Any]] = []
    for idx, left in enumerate(conditions):
        for right in conditions[idx + 1 :]:
            delta = paired_delta(
                by_condition[left], by_condition[right], seed=seed
            )
            if not delta["utterances"]:
                continue
            comparisons.append(
                {
                    "baseline_condition": left,
                    "candidate_condition": right,
                    **delta,
                }
            )
    return {
        "utterances": len(pairs),
        "conditions": {k: len(v) for k, v in sorted(by_condition.items())},
        "comparisons": comparisons,
    }


def paired_delta(
    baseline: Sequence[dict[str, Any]],
    candidate: Sequence[dict[str, Any]],
    *,
    metric: str = "wer_norm",
    weight: str = "ref_words_norm",
    seed: int = DEFAULT_SEED,
    iterations: int = BOOTSTRAP_ITERATIONS,
) -> dict[str, Any]:
    """Δ WER between two runs with a PAIRED bootstrap (§4 P1-5).

    Paired, not independent: the two runs scored the same utterances, and
    resampling them independently throws away that pairing and widens the
    interval for no reason. Each replicate draws a set of script ids and
    scores BOTH runs on that same set — the difference then has the variance
    of the difference, which is what the question "did this change help?"
    actually asks.

    A CI that straddles zero means the change is not distinguishable from
    resampling noise, however good the point estimate looks.
    """
    by_id_a = {str(i["script_id"]): i for i in baseline}
    by_id_b = {str(i["script_id"]): i for i in candidate}
    common = sorted(set(by_id_a) & set(by_id_b))
    if not common:
        return {
            "utterances": 0,
            "baseline": None,
            "candidate": None,
            "delta": None,
            "delta_ci": None,
            "significant": False,
            "improved": [],
            "regressed": [],
            "unchanged": 0,
        }

    a_items = [by_id_a[k] for k in common]
    b_items = [by_id_b[k] for k in common]
    a_value = _weighted(a_items, metric, weight)
    b_value = _weighted(b_items, metric, weight)

    rng = random.Random(seed)
    n = len(common)
    replicates: list[float] = []
    for _ in range(iterations):
        picks = [rng.randrange(n) for _ in range(n)]
        a_r = _weighted([a_items[p] for p in picks], metric, weight)
        b_r = _weighted([b_items[p] for p in picks], metric, weight)
        if a_r is not None and b_r is not None:
            replicates.append(b_r - a_r)

    ci: tuple[float, float] | None = None
    if replicates:
        replicates.sort()
        ci = (
            round(_percentile(replicates, _CI_LOW), 4),
            round(_percentile(replicates, _CI_HIGH), 4),
        )

    per_item: list[dict[str, Any]] = []
    for key in common:
        a_metric = by_id_a[key].get(metric)
        b_metric = by_id_b[key].get(metric)
        if a_metric is None or b_metric is None:
            continue
        per_item.append(
            {
                "script_id": key,
                "baseline": round(float(a_metric), 4),
                "candidate": round(float(b_metric), 4),
                "delta": round(float(b_metric) - float(a_metric), 4),
                "subset": by_id_a[key].get("subset"),
            }
        )
    moved = [i for i in per_item if i["delta"] != 0]
    improved = sorted(moved, key=lambda i: i["delta"])[:10]
    regressed = sorted(moved, key=lambda i: -i["delta"])[:10]

    return {
        "utterances": n,
        "baseline": _round(a_value),
        "candidate": _round(b_value),
        "delta": (
            None if a_value is None or b_value is None else round(b_value - a_value, 4)
        ),
        "delta_ci": ci,
        # The only honest yes/no this comparison can give.
        "significant": bool(ci and (ci[0] > 0 or ci[1] < 0)),
        "improved": improved,
        "regressed": regressed,
        "unchanged": len(per_item) - len(moved),
        "bootstrap": {"iterations": iterations, "seed": seed, "paired": True},
    }
