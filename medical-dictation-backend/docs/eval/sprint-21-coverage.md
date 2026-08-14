# Corpus coverage per release (sprint 21 §9)

One row per corpus release, produced by `scripts/eval/corpus_coverage.py`
on the frozen replay set (`eval/replay/replay-set-v1.json`).

Gates (non-negotiable, sprint 10 targets restated): **usefulness@3 ≥ 80%,
harmful = 0**. One harmful top-3 suggestion blocks the release.

Honesty note (sprint-05 retro #8): `synthetic` coverage replays prefix
truncations of the release corpus itself — it measures trie/ranking
self-consistency, NOT clinical coverage. The `telemetry` column replays
real zero-accept prefixes from `corpus-forge gaps` and is the number that
matters; it stays sparse until pilot telemetry accumulates
(docs/sprint-21/EXPLORE.md §3: dev telemetry = 8 events).

| release | generated | coverage@3 (synthetic) | coverage@3 (telemetry) | usefulness@3 | harmful |
| --- | --- | --- | --- | --- | --- |
| v0.0.1 | 2026-08-12T20:02:27+00:00 | 100.0% | n/a (no real prefixes in replay set) | pending clinician marks | 0 |
