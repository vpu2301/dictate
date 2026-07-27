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

Re-pinning without editing this file (the same contract the Whisper bake
offers via --build-arg, docs/models/PINS.md § Re-pinning):

    uv run python scripts/models/prepare_ecapa.py \
        --revision <new-commit> \
        --embedding-sha256 <new-embedding_model.ckpt-sha256> \
        --meanvar-sha256 <new-mean_var_norm_emb.ckpt-sha256>
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


def prepare(
    target: Path,
    *,
    revision: str = REVISION,
    pinned: dict[str, str] | None = None,
) -> Path:
    from huggingface_hub import snapshot_download  # lazy: needs network path only

    pinned = pinned or PINNED
    snapshot = Path(
        snapshot_download(REPO, revision=revision, allow_patterns=sorted(pinned))
    )
    target.mkdir(parents=True, exist_ok=True)
    for name, want in pinned.items():
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
    parser.add_argument("--revision", default=REVISION, help="immutable commit, never a tag")
    parser.add_argument("--embedding-sha256", default=PINNED["embedding_model.ckpt"])
    parser.add_argument("--meanvar-sha256", default=PINNED["mean_var_norm_emb.ckpt"])
    args = parser.parse_args()
    if args.revision != REVISION:
        print(
            f"WARNING: re-pinning to {args.revision} (default {REVISION}). "
            "Re-baselining the DER gate after a model change is ADR-gated (ADR-0034)."
        )
    prepare(
        args.target,
        revision=args.revision,
        pinned={
            "embedding_model.ckpt": args.embedding_sha256,
            "mean_var_norm_emb.ckpt": args.meanvar_sha256,
        },
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
