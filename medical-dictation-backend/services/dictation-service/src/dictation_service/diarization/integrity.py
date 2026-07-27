"""Startup integrity check for the baked diarization weights (sprint 14).

The build-time contract (docs/models/PINS.md) fetches ECAPA at an immutable
revision and verifies SHA-256 before baking. This module asserts the SAME
digests again **at process startup**, so a tampered, truncated, or
wrong-image layer is caught before the first patient consultation rather
than silently producing speaker labels from unknown weights.

Fail-closed: a mismatch raises. For a medical product, refusing to start is
strictly better than diarizing with weights nobody can account for.

Cost is ~0.2 s for the 83 MB artifact — paid once, off the request path.
"""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Artifact name -> the settings attribute carrying its pinned digest.
VERIFIED_ARTIFACTS: tuple[str, ...] = ("embedding_model.ckpt", "mean_var_norm_emb.ckpt")


class ModelIntegrityError(Exception):
    """A baked model artifact does not match its pinned digest."""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_model_dir(
    model_dir: str | Path,
    *,
    pins: dict[str, str],
    repo: str = "",
    revision: str = "",
) -> dict[str, str]:
    """Verify each pinned artifact under ``model_dir``.

    ``pins`` maps artifact filename -> expected sha256. An entry with an
    empty digest is treated as UNPINNED: its presence is still required,
    but the content is only logged, with a warning — that is the dev path
    (``make prepare-ecapa``), never a shipped image, because both
    Dockerfiles bake the digests as ENV.

    Returns the map of artifact -> actual digest for the caller to log.

    Raises ModelIntegrityError if a file is missing or a pinned digest
    does not match.
    """
    root = Path(model_dir)
    if not root.is_dir():
        raise ModelIntegrityError(
            f"diarization model dir not found: {root} "
            "(image should bake it at /opt/models/ecapa; dev: `make prepare-ecapa`)"
        )

    actual: dict[str, str] = {}
    unpinned: list[str] = []
    t0 = time.monotonic()
    for name, expected in pins.items():
        path = root / name
        if not path.is_file():
            raise ModelIntegrityError(f"missing diarization artifact: {path}")
        got = sha256_file(path)
        actual[name] = got
        if not expected:
            unpinned.append(name)
            continue
        if got != expected:
            # Do not log the file path's contents or the expected value as a
            # secret — these are public digests; the point is loud provenance.
            raise ModelIntegrityError(
                f"checksum mismatch for {path}: expected {expected}, got {got}. "
                "Refusing to start (fail-closed, docs/models/PINS.md)."
            )

    # hyperparams.yaml is repo-owned (infra/models/ecapa/) rather than
    # fetched, so it carries no upstream digest — but its ABSENCE means the
    # loader would try to resolve the model over the network, which is the
    # exact offline-hostile behaviour ADR-0034 rejected pyannote for.
    if not (root / "hyperparams.yaml").is_file():
        raise ModelIntegrityError(
            f"missing {root / 'hyperparams.yaml'} — without the repo-owned patched "
            "copy, SpeechBrain re-resolves the model over the network (ADR-0034)."
        )

    elapsed_ms = (time.monotonic() - t0) * 1000
    if unpinned:
        logger.warning(
            "diarization.model_unpinned",
            extra={
                "model_dir": str(root),
                "unpinned_artifacts": ",".join(unpinned),
                "detail": "no digest configured; verified presence only. "
                "Shipped images set MDX_DIAR_MODEL_SHA256 / MDX_DIAR_MEANVAR_SHA256.",
            },
        )
    logger.info(
        "diarization.model_verified",
        extra={
            "model_dir": str(root),
            "model_repo": repo or "(unset)",
            "model_revision": revision or "(unpinned)",
            "artifacts": ",".join(f"{k}={v[:12]}" for k, v in actual.items()),
            "verify_ms": round(elapsed_ms, 1),
        },
    )
    return actual
