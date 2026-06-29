"""Pure scoring + corpus-integrity helpers for the sprint-07 WER gate.

Stdlib-only on purpose: every function here is deterministic and
unit-testable without a GPU, faster-whisper, or a database. The heavy
Whisper inference + DB persistence lives in ``run_wer.py``; the
measurement *contract* (tokenisation, Levenshtein, CER, number-norm,
manifest SHA-256) lives here so it can be regression-protected by tests.

See ``docs/eval/wer-methodology.md`` — this module is its executable
form.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path

# ── Tokenisation ──────────────────────────────────────────────────────
# Lowercase + strip non-alphanumeric, Unicode-aware so Cyrillic survives.
# We deliberately do NOT strip Ukrainian case endings: "інфаркту" vs
# "інфаркт" is a real error and must be counted (methodology §WER).
_TOKEN_STRIP = re.compile(r"[^\w']+", flags=re.UNICODE)


def tokenize(text: str) -> list[str]:
    """Lowercase, strip punctuation (keep apostrophes), whitespace split."""
    text = _TOKEN_STRIP.sub(" ", text.lower())
    return text.split()


# ── Levenshtein over an arbitrary sequence ────────────────────────────
def _levenshtein(ref: list[str], hyp: list[str]) -> int:
    n, m = len(ref), len(hyp)
    if n == 0:
        return m
    if m == 0:
        return n
    prev = list(range(m + 1))
    for i in range(1, n + 1):
        curr = [i] + [0] * m
        for j in range(1, m + 1):
            cost = 0 if ref[i - 1] == hyp[j - 1] else 1
            curr[j] = min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost)
        prev = curr
    return prev[m]


def wer(reference: str, hypothesis: str) -> tuple[float, int]:
    """Levenshtein WER. Returns ``(wer, reference_word_count)``.

    Edge case (methodology §Edge cases): empty reference scores 0.0 when
    the hypothesis is also empty, else 1.0 (full insertion).
    """
    ref = tokenize(reference)
    hyp = tokenize(hypothesis)
    n = len(ref)
    if n == 0:
        return (0.0 if not hyp else 1.0), 0
    return _levenshtein(ref, hyp) / n, n


def cer(reference: str, hypothesis: str) -> tuple[float, int]:
    """Character error rate. Case-insensitive, spaces preserved.

    Returns ``(cer, reference_char_count)``. Tiebreaker for utterances
    dominated by a single substitution (methodology §CER).
    """
    ref = list(reference.lower())
    hyp = list(hypothesis.lower())
    n = len(ref)
    if n == 0:
        return (0.0 if not hyp else 1.0), 0
    return _levenshtein(ref, hyp) / n, n


# ── Number normalisation (per-category accuracy) ──────────────────────
# Each category extracts the literal instances from a transcript; the
# score is the multiset overlap |ref ∩ hyp| / |ref| (methodology
# §Number-norm score). Categories: BP / dose / frequency / date.
NUMBER_CATEGORIES: dict[str, re.Pattern[str]] = {
    # Blood pressure: systolic/diastolic, e.g. 120/80.
    "bp": re.compile(r"\b\d{2,3}/\d{2,3}\b"),
    # Dose: a number followed by a unit (mg, ml, g, mcg, units, од…).
    "dose": re.compile(
        r"\b\d+(?:[.,]\d+)?\s*(?:мг|mg|мл|ml|мкг|mcg|г|g|од|units?|u)\b",
        re.IGNORECASE | re.UNICODE,
    ),
    # Frequency: a number near a repetition word, e.g. "2 рази", "3 times".
    "frequency": re.compile(
        r"\b\d+\s*(?:раз(?:и|а|ів|у)?|times?|/добу|/день|/day)\b",
        re.IGNORECASE | re.UNICODE,
    ),
    # Date: dd.mm.yyyy / dd/mm/yy / ISO yyyy-mm-dd.
    "date": re.compile(
        r"\b\d{1,2}[./]\d{1,2}[./]\d{2,4}\b|\b\d{4}-\d{2}-\d{2}\b"
    ),
}


def extract_numbers(text: str) -> dict[str, list[str]]:
    """Return category → list of normalised matched instances."""
    out: dict[str, list[str]] = {}
    for cat, rx in NUMBER_CATEGORIES.items():
        matches = [re.sub(r"\s+", "", m.group(0).lower()) for m in rx.finditer(text)]
        if matches:
            out[cat] = matches
    return out


def _overlap(ref_items: list[str], hyp_items: list[str]) -> int:
    """Multiset intersection size."""
    return sum((Counter(ref_items) & Counter(hyp_items)).values())


def number_norm_by_category(reference: str, hypothesis: str) -> dict[str, float]:
    """Per-category accuracy. Categories absent from the reference are
    omitted (no reference instance → nothing to score)."""
    ref = extract_numbers(reference)
    hyp = extract_numbers(hypothesis)
    scores: dict[str, float] = {}
    for cat, ref_items in ref.items():
        matched = _overlap(ref_items, hyp.get(cat, []))
        scores[cat] = matched / len(ref_items)
    return scores


def number_norm_overall(reference: str, hypothesis: str) -> float | None:
    """Combined accuracy across all categories. ``None`` when the
    reference contains no categorised numbers (stored as NULL so the
    gate's ``number_norm_score IS NOT NULL`` filter skips it)."""
    ref = extract_numbers(reference)
    if not ref:
        return None
    hyp = extract_numbers(hypothesis)
    total_ref = sum(len(v) for v in ref.values())
    total_matched = sum(_overlap(v, hyp.get(cat, [])) for cat, v in ref.items())
    return total_matched / total_ref


# ── Percentiles (RTF p50 / p95) ───────────────────────────────────────
def percentile(values: list[float], p: float) -> float:
    """Linear-interpolation percentile. ``p`` in [0, 100]."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (p / 100.0) * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    frac = rank - low
    return ordered[low] + (ordered[high] - ordered[low]) * frac


# ── Corpus manifest integrity (SHA-256) ───────────────────────────────
_MANIFEST_FILES = ("audio.wav", "transcript.txt", "metadata.json")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def build_manifest(corpus_dir: Path) -> dict:
    """Scan ``corpus_dir`` for utterance subdirs and build the manifest
    dict (preserving any top-level metadata already present)."""
    existing: dict = {}
    manifest_path = corpus_dir / "manifest.json"
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text("utf-8"))

    utterances: list[dict] = []
    for sub in sorted(p for p in corpus_dir.iterdir() if p.is_dir()):
        meta_path = sub / "metadata.json"
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text("utf-8"))
        utterances.append(
            {
                "utterance_id": meta["utterance_id"],
                "language": meta["language"],
                "specialty": meta["specialty"],
                "duration_s": meta["duration_s"],
                "dictation_source": meta.get("dictation_source", "unknown"),
                "path": sub.name,
                "sha256": {f: sha256_file(sub / f) for f in _MANIFEST_FILES},
            }
        )

    return {
        "corpus_version": existing.get("corpus_version", corpus_dir.name),
        "schema_version": existing.get("schema_version", 1),
        "description": existing.get("description", ""),
        "license": existing.get("license", "internal-use-only"),
        "utterances": utterances,
    }


def verify_manifest(corpus_dir: Path, manifest: dict | None = None) -> list[str]:
    """Recompute hashes and compare to the manifest. Returns a list of
    human-readable mismatch messages (empty list = integrity OK)."""
    if manifest is None:
        manifest = json.loads((corpus_dir / "manifest.json").read_text("utf-8"))
    problems: list[str] = []
    for entry in manifest.get("utterances", []):
        uid = entry["utterance_id"]
        sub = corpus_dir / entry["path"]
        if not sub.is_dir():
            problems.append(f"{uid}: directory {entry['path']} missing")
            continue
        for fname, expected in entry["sha256"].items():
            fpath = sub / fname
            if not fpath.exists():
                problems.append(f"{uid}: {fname} missing")
                continue
            actual = sha256_file(fpath)
            if actual != expected:
                problems.append(
                    f"{uid}: {fname} sha256 mismatch "
                    f"(manifest {expected[:12]}… ≠ actual {actual[:12]}…)"
                )
    return problems
