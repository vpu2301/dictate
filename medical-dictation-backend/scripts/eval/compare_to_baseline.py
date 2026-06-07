"""Sprint-07 — compare the most-recent eval_run to audit.eval_baseline.

Used in the nightly CI job after run_wer.py. Exits non-zero if any
configured threshold is breached.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import asyncpg


async def compare(args: argparse.Namespace) -> int:
    conn = await asyncpg.connect(args.dsn)
    try:
        baseline = await conn.fetchrow(
            "SELECT wer_overall_uk, wer_overall_en, rtf_p95 FROM audit.eval_baseline WHERE id = 1"
        )
        if baseline is None:
            print("warn: no baseline established yet — skipping comparison")
            return 0
        latest = await conn.fetchrow(
            "SELECT id, started_at, wer_overall_uk, wer_overall_en, rtf_p95 "
            "FROM audit.eval_runs ORDER BY started_at DESC LIMIT 1"
        )
        if latest is None:
            print("error: no runs to compare", file=sys.stderr)
            return 2

        failures: list[str] = []
        for lang in ("uk", "en"):
            col = f"wer_overall_{lang}"
            delta = float(latest[col]) - float(baseline[col])
            if delta > args.threshold_wer_pp / 100.0:
                failures.append(
                    f"WER {lang} regressed: baseline {baseline[col]:.4f} -> "
                    f"{latest[col]:.4f} (Δ={delta * 100:+.2f} pp, threshold "
                    f"{args.threshold_wer_pp:+.2f} pp)"
                )
        rtf_delta = float(latest["rtf_p95"]) - float(baseline["rtf_p95"])
        if rtf_delta > args.threshold_rtf:
            failures.append(
                f"RTF p95 regressed: baseline {baseline['rtf_p95']:.3f} -> "
                f"{latest['rtf_p95']:.3f} (Δ={rtf_delta:+.3f}, threshold "
                f"{args.threshold_rtf:+.3f})"
            )

        # Per-category number-norm thresholds: compute from utterances.
        rows = await conn.fetch(
            "SELECT number_norm_score, language FROM audit.eval_utterances "
            "WHERE run_id = $1 AND number_norm_score IS NOT NULL",
            latest["id"],
        )
        if rows:
            avg = sum(float(r["number_norm_score"]) for r in rows) / len(rows)
            if avg < args.threshold_number_norm:
                failures.append(
                    f"number-norm avg {avg:.4f} below threshold {args.threshold_number_norm:.4f}"
                )

        if failures:
            print("=== regression detected ===", file=sys.stderr)
            for f in failures:
                print("  " + f, file=sys.stderr)
            return 1
        print("ok: no regression vs baseline")
        return 0
    finally:
        await conn.close()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dsn", required=True)
    p.add_argument(
        "--threshold-wer-pp",
        type=float,
        default=1.0,
        help="Max WER regression in percentage points",
    )
    p.add_argument(
        "--threshold-rtf",
        type=float,
        default=0.05,
        help="Max RTF p95 regression (lower is worse so this is +Δ)",
    )
    p.add_argument("--threshold-number-norm", type=float, default=0.95)
    args = p.parse_args()
    return asyncio.run(compare(args))


if __name__ == "__main__":
    sys.exit(main())
