#!/usr/bin/env python3
"""Regenerate ``manifest.json`` for a WER corpus version.

Run after adding / replacing utterances. The manifest's SHA-256 entries
are what the nightly gate (and the integrity test) verify on every PR —
tampering with a ``.wav`` after the manifest is committed is caught.

Usage::

    python scripts/eval/build_corpus_manifest.py --corpus eval/corpus/v1
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wer_lib  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--corpus", type=Path, default=Path("eval/corpus/v1"))
    p.add_argument(
        "--check",
        action="store_true",
        help="Verify the existing manifest instead of rewriting it; "
        "exit non-zero on any integrity mismatch.",
    )
    args = p.parse_args()

    if args.check:
        problems = wer_lib.verify_manifest(args.corpus)
        if problems:
            print("=== corpus integrity FAILED ===", file=sys.stderr)
            for prob in problems:
                print("  " + prob, file=sys.stderr)
            return 1
        print(f"ok: corpus {args.corpus} integrity verified")
        return 0

    manifest = wer_lib.build_manifest(args.corpus)
    out = args.corpus / "manifest.json"
    out.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {out} ({len(manifest['utterances'])} utterances)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
