#!/usr/bin/env python3
"""Fetch terminology source snapshots (sprint 21 deployment plan).

Downloads every dataset listed in ``infra/seeds/corpus/sources.urls`` into
``infra/seeds/corpus/sources/`` (gitignored) and records name, URL, SHA-256
and pull date in ``infra/seeds/corpus/sources.lock`` (committed). The
lockfile is the provenance record; the data is not committed.

``corpus-forge import --dataset <name> --dataset-version <date> --file
infra/seeds/corpus/sources/<name>.<ext>`` then carries the same SHA into
every candidate's ``source_ref``.

Licence basis per source: docs/sprint-21/EXPLORE.md §2 — production imports
run only after the DPO countersign (docs/signoffs/sprint-21-dpo.md).
"""

from __future__ import annotations

import hashlib
import json
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
URLS_FILE = ROOT / "infra/seeds/corpus/sources.urls"
DEST_DIR = ROOT / "infra/seeds/corpus/sources"
LOCK_FILE = ROOT / "infra/seeds/corpus/sources.lock"


def main() -> int:
    if not URLS_FILE.exists():
        print(f"missing {URLS_FILE} — see its template in git", file=sys.stderr)
        return 2
    entries = []
    for line in URLS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            name, url = line.split(None, 1)
            entries.append((name, url.strip()))
    if not entries:
        print(
            "sources.urls has no active entries — fill in the dataset URLs "
            "(operator step; see file comments)",
            file=sys.stderr,
        )
        return 2

    DEST_DIR.mkdir(parents=True, exist_ok=True)
    lock: dict[str, dict[str, str]] = {}
    if LOCK_FILE.exists():
        lock = json.loads(LOCK_FILE.read_text(encoding="utf-8"))

    for name, url in entries:
        suffix = Path(url.split("?", 1)[0]).suffix or ".csv"
        dest = DEST_DIR / f"{name}{suffix}"
        print(f"fetch {name} ← {url}")
        with urllib.request.urlopen(url, timeout=120) as resp:  # noqa: S310 - operator-curated URL list
            data = resp.read()
        dest.write_bytes(data)
        lock[name] = {
            "url": url,
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": str(len(data)),
            "fetched_at": datetime.now(tz=UTC).isoformat(timespec="seconds"),
        }
        print(f"  → {dest.name}  sha256={lock[name]['sha256']}")

    LOCK_FILE.write_text(
        json.dumps(lock, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"lockfile updated: {LOCK_FILE.relative_to(ROOT)} (commit it; the data stays local)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
