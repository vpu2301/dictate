"""Streaming diarization eval over eval/conversations/v1 (sprint 14).

Replays each dialogue through the PRODUCTION diarization stream
(``dictation_service.diarization``) at the exact ADR-0013 cadence
(4 s window / 2 s stride), then scores:

  * DER (frame-based, ±250 ms collar, optimal label mapping)
  * word-level attribution vs the generator's exact turn boundaries
  * UNKNOWN rate (the honesty metric — ambiguity must surface, not hide)
  * per-window diarization latency (VAD + embed + cluster)
  * doctor/patient mapping-hint correctness per dialogue

Numbers from this laptop (CPU) are plumbing + relative-quality signal;
the release gate re-runs the same command on the A10G rig
(docs/eval/der-methodology.md), mirroring the WER precedent.

Usage:
    uv run python scripts/eval/run_der.py [--corpus eval/conversations/v1]
                                          [--json out.json]
"""

from __future__ import annotations

import argparse
import json
import resource
import statistics
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "eval"))
sys.path.insert(0, str(REPO_ROOT / "services" / "dictation-service" / "src"))

import der_lib  # noqa: E402
import numpy as np  # noqa: E402

SAMPLE_RATE = 16_000
WINDOW_MS = 4_000
STRIDE_MS = 2_000

DEFAULT_MODEL_DIR = Path.home() / ".cache" / "mdx-models" / "ecapa-voxceleb"

# Pilot bars (record actuals in ADR-0034; the rig re-run is the gate).
DER_BAR = 0.20
ATTRIBUTION_BAR = 0.85


def _load_wav(path: Path) -> np.ndarray:
    import wave

    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == SAMPLE_RATE and w.getnchannels() == 1
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def evaluate_dialogue(d: Path, embedder, make_segmenter) -> dict:
    from dictation_service.diarization import DiarizationStream
    from dictation_service.diarization.mapping import SpeakerMappingInference

    ref = json.loads((d / "reference.json").read_text(encoding="utf-8"))
    audio = _load_wav(d / "audio.wav")
    total_ms = audio.shape[0] * 1000 // SAMPLE_RATE

    stream = DiarizationStream(embedder=embedder, segmenter=make_segmenter())
    latencies: list[float] = []
    cursor = 0
    while cursor < total_ms:
        start = max(0, cursor - (WINDOW_MS - STRIDE_MS))
        end = min(total_ms, start + WINDOW_MS)
        pcm = audio[start * SAMPLE_RATE // 1000 : end * SAMPLE_RATE // 1000]
        stream.process_window(pcm, window_start_ms=start)
        latencies.append(stream.last_window_seconds * 1000)
        cursor = end
        if end >= total_ms:
            break

    two_spk_turns = [t for t in ref["turns"] if t["speaker"] in ("A", "B")]
    hyp = [(s.start_ms, s.end_ms, s.label) for s in stream.segments]
    der = der_lib.compute_der(two_spk_turns, hyp, duration_ms=total_ms)

    words = der_lib.pseudo_words(two_spk_turns)
    attributions = [stream.attribute(w["start_ms"], w["end_ms"]) for w in words]
    attr = der_lib.score_attribution(words, attributions, der.mapping)

    # Third-voice honesty: words of speaker C must never get S1/S2.
    c_turns = [t for t in ref["turns"] if t["speaker"] == "C"]
    c_words = der_lib.pseudo_words(c_turns)
    c_attr = [stream.attribute(w["start_ms"], w["end_ms"]) for w in c_words]
    c_guessed = sum(1 for label, _ in c_attr if label in ("S1", "S2"))

    # Mapping inference on the attributed pseudo-words (stream order).
    inference = SpeakerMappingInference(language=ref["language"])
    inference.observe_segments(stream.segments)
    hint = None
    for w, (label, _conf) in zip(words, attributions, strict=True):
        inference.observe_word(w["text"], label)
        h = inference.evaluate()
        if h is not None:
            hint = h
    role_by_ref = {s: ref["roles"][s] for s in ("A", "B") if s in ref["roles"]}
    hint_correct = None
    if hint is not None:
        # hint maps hyp labels -> roles; ground truth via der.mapping.
        hint_correct = all(
            role_by_ref.get(der.mapping.get(hyp_label, "?")) == role
            for hyp_label, role in hint.mapping.items()
        )

    return {
        "dialogue_id": ref["dialogue_id"],
        "language": ref["language"],
        "duration_ms": total_ms,
        "num_ref_speakers": ref["num_speakers"],
        "hyp_speaker_count": stream.speaker_count,
        "der": der.der,
        "der_miss": der.miss,
        "der_false_alarm": der.false_alarm,
        "der_confusion": der.confusion,
        "der_ref_speech_ms": der.ref_speech_ms,
        "attribution_strict_accuracy": attr.strict_accuracy,
        "attribution_labeled_precision": attr.labeled_precision,
        "attribution_unknown_rate": attr.unknown_rate,
        "attribution_pending": attr.pending,
        "words_total": attr.total_words,
        "third_voice_words": len(c_words),
        "third_voice_wrongly_labeled": c_guessed,
        "window_latency_ms_p50": round(statistics.median(latencies), 1),
        "window_latency_ms_p95": round(sorted(latencies)[int(0.95 * (len(latencies) - 1))], 1),
        "window_latency_ms_max": round(max(latencies), 1),
        "windows": len(latencies),
        "mapping_hint": (hint.mapping if hint else None),
        "mapping_hint_confidence": (hint.confidence if hint else None),
        "mapping_hint_correct": hint_correct,
        "notes": ref.get("notes", ""),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=REPO_ROOT / "eval" / "conversations" / "v1")
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()

    if not args.model_dir.is_dir():
        raise SystemExit(
            f"ECAPA model dir missing: {args.model_dir}\n"
            "run: uv run python scripts/models/prepare_ecapa.py"
        )

    from dictation_service.diarization import EcapaEmbedder, SileroSegmenter

    embedder = EcapaEmbedder(model_dir=str(args.model_dir), device=args.device)
    embedder.warm_up()

    results = []
    for d in sorted(p for p in args.corpus.iterdir() if (p / "reference.json").exists()):
        r = evaluate_dialogue(d, embedder, SileroSegmenter)
        results.append(r)
        hint = r["mapping_hint_correct"]
        print(
            f"{r['dialogue_id']:<24} DER={r['der']:.3f} "
            f"attr={r['attribution_strict_accuracy']:.3f} "
            f"unk={r['attribution_unknown_rate']:.3f} "
            f"lat_p95={r['window_latency_ms_p95']:.0f}ms "
            f"hint={'—' if hint is None else ('OK' if hint else 'WRONG')}"
        )

    two_spk = [r for r in results if r["num_ref_speakers"] == 2]
    # Corpus-level DER: total error time over total reference speech —
    # the standard multi-file aggregation (a 13 s stress dialogue must
    # not carry the same weight as a 35 s consult).
    total_ref = sum(r["der_ref_speech_ms"] for r in two_spk)
    corpus_der = sum(r["der"] * r["der_ref_speech_ms"] for r in two_spk) / max(1, total_ref)
    total_words = sum(r["words_total"] for r in two_spk)
    corpus_attr = sum(
        r["attribution_strict_accuracy"] * r["words_total"] for r in two_spk
    ) / max(1, total_words)
    summary = {
        "corpus": str(args.corpus),
        "device": args.device,
        "dialogues": len(results),
        "der_corpus_2spk": round(corpus_der, 4),
        "der_mean_2spk": round(statistics.mean(r["der"] for r in two_spk), 4),
        "der_worst_2spk": max(r["der"] for r in two_spk),
        "attribution_corpus": round(corpus_attr, 4),
        "attribution_worst": min(r["attribution_strict_accuracy"] for r in two_spk),
        "unknown_rate_mean": round(
            statistics.mean(r["attribution_unknown_rate"] for r in results), 4
        ),
        "window_latency_ms_p95_max": max(r["window_latency_ms_p95"] for r in results),
        "third_voice_wrongly_labeled_total": sum(r["third_voice_wrongly_labeled"] for r in results),
        "max_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1 << 20), 1),
        "bars": {"der": DER_BAR, "attribution": ATTRIBUTION_BAR},
        "der_bar_met": corpus_der <= DER_BAR,
        "attribution_bar_met": corpus_attr >= ATTRIBUTION_BAR,
        "results": results,
    }
    print(
        f"\n2-speaker corpus DER={summary['der_corpus_2spk']:.3f} "
        f"(mean={summary['der_mean_2spk']:.3f}, worst={summary['der_worst_2spk']:.3f}; bar {DER_BAR}) "
        f"-> {'PASS' if summary['der_bar_met'] else 'FAIL'}"
    )
    print(
        f"attribution corpus={summary['attribution_corpus']:.3f} "
        f"(worst={summary['attribution_worst']:.3f}; bar {ATTRIBUTION_BAR}) "
        f"-> {'PASS' if summary['attribution_bar_met'] else 'FAIL'}"
    )
    print(
        f"third-voice words wrongly labeled S1/S2: {summary['third_voice_wrongly_labeled_total']} "
        f"| latency p95 max={summary['window_latency_ms_p95_max']:.0f}ms "
        f"| max RSS={summary['max_rss_mb']}MB"
    )
    if args.json:
        args.json.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
        print(f"wrote {args.json}")
    return 0 if (summary["der_bar_met"] and summary["attribution_bar_met"]) else 1


if __name__ == "__main__":
    sys.exit(main())
