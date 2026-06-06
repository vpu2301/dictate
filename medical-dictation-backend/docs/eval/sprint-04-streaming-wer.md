# Sprint 04 — Streaming WER vs Batch WER

This document records the streaming-vs-batch WER comparison for the
sprint-04 demo and the nightly job afterwards.

## Targets (parity within 1 absolute point of sprint-03 batch)

| Language | Specialty   | Batch (sprint 03) | Streaming target (sprint 04) |
| -------- | ----------- | ----------------- | ---------------------------- |
| uk       | general     | ≤ 0.18            | ≤ 0.19                       |
| uk       | cardiology  | ≤ 0.14            | ≤ 0.15                       |
| en       | general     | ≤ 0.10            | ≤ 0.11                       |
| en       | cardiology  | ≤ 0.08            | ≤ 0.09                       |

## How to run

```sh
make wer-eval-streaming
# or
uv run python scripts/eval/run_streaming_wer.py \
    --fixtures tests/fixtures/wer \
    --fail-on-regression
```

## Demo result (2026-06-09)

(Recorded post-demo; the table mirrors the harness output.)

| Language | Specialty   | Files | Ref words | Batch WER | Streaming WER | Δ |
| -------- | ----------- | ----- | --------- | --------- | ------------- | - |
| uk       | general     |       |           |           |               |   |
| uk       | cardiology  |       |           |           |               |   |
| en       | general     |       |           |           |               |   |
| en       | cardiology  |       |           |           |               |   |
