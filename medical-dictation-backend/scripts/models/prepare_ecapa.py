"""Assemble the pinned ECAPA speaker-embedding model dir (sprint 14, ADR-0034).

Mirrors the Whisper/punctuation bake contract (docs/models/PINS.md): fetch at
an immutable revision, verify SHA-256 fail-closed, and produce a directory
that loads FULLY OFFLINE. Used both by developers (default target under
~/.cache/mdx-models) and by the Dockerfile model-fetch stage (target
/opt/models/ecapa).

The directory layout it produces:

    <target>/
      hyperparams.yaml        <- repo-owned patched copy (infra/models/ecapa/)
      embedding_model.ckpt    <- upstream artifact, checksum-verified
      mean_var_norm_emb.ckpt  <- upstream artifact, checksum-verified

Usage:
    uv run python scripts/models/prepare_ecapa.py [--target DIR]
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

REPO = "speechbrain/spkrec-ecapa-voxceleb"
# Immutable commit, resolved 2026-07-26 (docs/models/PINS.md).
REVISION = "0f99f2d0ebe89ac095bcc5903c4dd8f72b367286"

# artifact -> pinned SHA-256. A mismatch fails the run — never bake anyway.
PINNED: dict[str, str] = {
    "embedding_model.ckpt": "0575cb64845e6b9a10db9bcb74d5ac32b326b8dc90352671d345e2ee3d0126a2",
    "mean_var_norm_emb.ckpt": "cd70225b05b37be64fc5a95e24395d804231d43f74b2e1e5a513db7b69b34c33",
}

_REPO_ROOT = Path(__file__).resolve().parents[2]
PATCHED_HPARAMS = _REPO_ROOT / "infra" / "models" / "ecapa" / "hyperparams.yaml"
DEFAULT_TARGET = Path.home() / ".cache" / "mdx-models" / "ecapa-voxceleb"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def prepare(target: Path) -> Path:
    from huggingface_hub import snapshot_download  # lazy: needs network path only

    snapshot = Path(
        snapshot_download(REPO, revision=REVISION, allow_patterns=sorted(PINNED))
    )
    target.mkdir(parents=True, exist_ok=True)
    for name, want in PINNED.items():
        src = snapshot / name
        got = _sha256(src)
        if got != want:
            raise SystemExit(
                f"FATAL: checksum mismatch for {name}: expected {want}, got {got}. "
                "Refusing to install (fail-closed, docs/models/PINS.md)."
            )
        shutil.copyfile(src, target / name)
        print(f"  {name}  sha256={got}  OK")
    shutil.copyfile(PATCHED_HPARAMS, target / "hyperparams.yaml")
    print(f"  hyperparams.yaml  (patched, from {PATCHED_HPARAMS.relative_to(_REPO_ROOT)})")
    print(f"ECAPA model dir ready: {target}")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    args = parser.parse_args()
    prepare(args.target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
