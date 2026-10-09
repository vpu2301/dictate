#!/usr/bin/env python3
"""Corpus coverage/usefulness harness — restate the sprint-10 gate as a
script (sprint 21 §9). Run per corpus release:

    uv run --project services/autocomplete-service \
        python scripts/eval/corpus_coverage.py \
        --release-dir infra/seeds/corpus/releases/v1.0.0 \
        --replay-set eval/replay/replay-set-v1.json \
        [--marks eval/replay/marks-v1.0.0.csv] \
        [--metrics-file /tmp/corpus_coverage.prom] \
        [--history docs/eval/sprint-21-coverage.md]

Measures, on a FIXED replay set (comparable release over release):
  * coverage@3   — % of replay prefixes returning ≥1 candidate in the top 3
  * usefulness@3 — % of covered prefixes whose top-3 the clinician marked
                   useful (from --marks; 'pending' without it)
  * harm rate    — any top-3 suggestion marked harmful. Gate: ZERO.
                   One harmful suggestion blocks the release.

Replay-set provenance matters (sprint-05 retro #8, "meaningful or theatre?"):
`--build-replay-set` freezes prefix truncations of the release corpus plus
any real telemetry prefixes piped in — the manifest of the JSON records
which is which, and the report keeps synthetic and real coverage separate.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from autocomplete_service.suggest import suggest_from_trie
from autocomplete_service.trie.builder import PhraseTrieEntry, build_trie_from_phrases

TOP_K = 3
GATE_USEFUL = 0.80
NIL = "00000000-0000-0000-0000-000000000000"


def load_release_entries(release_dir: Path) -> dict[str, list[PhraseTrieEntry]]:
    """Release phrases.csv → per-language trie entries (counters zero: a
    fresh release has no engagement history; ranking is source/length only)."""
    text = (release_dir / "phrases.csv").read_text(encoding="utf-8")
    by_language: dict[str, list[PhraseTrieEntry]] = {}
    for i, row in enumerate(csv.DictReader(io.StringIO(text))):
        entry = PhraseTrieEntry(
            id=str(i),
            phrase=row["phrase"],
            source="system",
            impression_count=0,
            acceptance_count=0,
            last_accepted_at=None,
            specialty=row["specialty"] or None,
            section_hint=row["section_hint"] or None,
        )
        by_language.setdefault(row["language"], []).append(entry)
    return by_language


def build_replay_set(
    by_language: dict[str, list[PhraseTrieEntry]],
    real_prefixes: dict[str, list[str]],
) -> dict[str, object]:
    """Freeze a replay set: 3/5/8-char truncations of every corpus phrase
    (synthetic, self-consistency only) + real telemetry prefixes."""
    items: list[dict[str, str]] = []
    for language, entries in sorted(by_language.items()):
        seen: set[str] = set()
        for e in entries:
            for n in (3, 5, 8):
                prefix = e.phrase[:n].strip()
                if len(prefix) >= 3 and prefix.lower() not in seen:
                    seen.add(prefix.lower())
                    items.append({"language": language, "prefix": prefix, "origin": "synthetic"})
        for prefix in real_prefixes.get(language, []):
            items.append({"language": language, "prefix": prefix, "origin": "telemetry"})
    return {
        "schema_version": 1,
        "note": "synthetic prefixes measure trie/ranking self-consistency, "
        "NOT clinical coverage; telemetry prefixes measure the real gap",
        "prefixes": items,
    }


def evaluate(
    by_language: dict[str, list[PhraseTrieEntry]],
    replay: dict[str, object],
    marks: dict[str, str],
) -> dict[str, object]:
    tries = {
        language: build_trie_from_phrases(
            tenant_id=NIL, language=language, user_id=NIL, rows=entries
        )
        for language, entries in by_language.items()
    }
    per_origin: dict[str, dict[str, int]] = {}
    harmful: list[str] = []
    useful = judged = 0
    prefixes = replay["prefixes"]
    assert isinstance(prefixes, list)
    for item in prefixes:
        language, prefix, origin = item["language"], item["prefix"], item["origin"]
        bucket = per_origin.setdefault(origin, {"total": 0, "covered": 0})
        bucket["total"] += 1
        trie = tries.get(language)
        suggestions = (
            suggest_from_trie(trie=trie, prefix=prefix, limit=TOP_K) if trie else []
        )
        if suggestions:
            bucket["covered"] += 1
        verdict = marks.get(prefix.lower())
        if verdict:
            judged += 1
            if verdict == "useful":
                useful += 1
            elif verdict == "harmful":
                harmful.append(prefix)

    coverage = {
        origin: (counts["covered"] / counts["total"] if counts["total"] else 0.0)
        for origin, counts in per_origin.items()
    }
    return {
        "generated_at": datetime.now(tz=UTC).isoformat(timespec="seconds"),
        "coverage_at_3": coverage,
        "counts": per_origin,
        "usefulness_at_3": (useful / judged) if judged else None,
        "judged": judged,
        "harmful": harmful,
        "harm_rate": (len(harmful) / judged) if judged else None,
    }


def write_metrics(path: Path, release: str, report: dict[str, object]) -> None:
    lines = []
    coverage = report["coverage_at_3"]
    assert isinstance(coverage, dict)
    for origin, value in sorted(coverage.items()):
        lines.append(
            f'mdx_corpus_coverage_at_3{{release="{release}",origin="{origin}"}} {value:.4f}'
        )
    usefulness = report["usefulness_at_3"]
    if usefulness is not None:
        lines.append(f'mdx_corpus_usefulness_at_3{{release="{release}"}} {usefulness:.4f}')
    harmful = report["harmful"]
    assert isinstance(harmful, list)
    lines.append(f'mdx_corpus_harmful_suggestions{{release="{release}"}} {len(harmful)}')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def append_history(path: Path, release: str, report: dict[str, object]) -> None:
    coverage = report["coverage_at_3"]
    assert isinstance(coverage, dict)
    synthetic = coverage.get("synthetic")
    telemetry = coverage.get("telemetry")
    usefulness = report["usefulness_at_3"]
    harmful = report["harmful"]
    assert isinstance(harmful, list)
    row = (
        f"| {release} | {report['generated_at']} "
        f"| {synthetic if synthetic is None else f'{synthetic:.1%}'} "
        f"| {telemetry if telemetry is None else f'{telemetry:.1%}'} "
        f"| {'pending' if usefulness is None else f'{usefulness:.1%}'} "
        f"| {len(harmful)} |\n"
    )
    with path.open("a", encoding="utf-8") as fh:
        fh.write(row)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-dir", required=True)
    parser.add_argument("--replay-set", required=True)
    parser.add_argument("--build-replay-set", action="store_true")
    parser.add_argument("--telemetry-prefixes", help="JSON {language: [prefix,…]} of real gaps")
    parser.add_argument("--marks", help="CSV prefix,verdict (useful|neutral|harmful)")
    parser.add_argument("--metrics-file")
    parser.add_argument("--history")
    args = parser.parse_args()

    release_dir = Path(args.release_dir)
    release = release_dir.name
    by_language = load_release_entries(release_dir)

    replay_path = Path(args.replay_set)
    if args.build_replay_set:
        real: dict[str, list[str]] = {}
        if args.telemetry_prefixes:
            real = json.loads(Path(args.telemetry_prefixes).read_text(encoding="utf-8"))
        replay_path.parent.mkdir(parents=True, exist_ok=True)
        replay_path.write_text(
            json.dumps(build_replay_set(by_language, real), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"replay set frozen: {replay_path}")

    replay = json.loads(replay_path.read_text(encoding="utf-8"))

    marks: dict[str, str] = {}
    if args.marks:
        with Path(args.marks).open(encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                marks[row["prefix"].lower()] = row["verdict"]

    report = evaluate(by_language, replay, marks)
    print(json.dumps(report, ensure_ascii=False, indent=2))

    if args.metrics_file:
        write_metrics(Path(args.metrics_file), release, report)
    if args.history:
        append_history(Path(args.history), release, report)

    harmful = report["harmful"]
    assert isinstance(harmful, list)
    if harmful:
        print(f"GATE FAIL: {len(harmful)} harmful top-3 suggestion(s) — release blocked", file=sys.stderr)
        return 2
    usefulness = report["usefulness_at_3"]
    if usefulness is not None and usefulness < GATE_USEFUL:
        print(f"GATE FAIL: usefulness@3 {usefulness:.1%} < {GATE_USEFUL:.0%}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
