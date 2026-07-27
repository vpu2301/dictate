"""Startup integrity assertion for the baked diarization weights (sprint 14).

The build verifies the ECAPA digests before baking; this re-asserts them at
process startup so a tampered or truncated layer is caught before the first
consultation. Fail-closed is the contract: every failure mode below must
RAISE, never warn-and-continue — diarizing with unaccountable weights is
worse than not starting.

Synthetic files, real digests: the point under test is the verification
logic, not the 83 MB artifact (that path is exercised live in
docs/runbooks/dictation.md § verify the baked models).
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import pytest

from dictation_service.diarization.integrity import (
    ModelIntegrityError,
    sha256_file,
    verify_model_dir,
)

EMBEDDING = "embedding_model.ckpt"
MEANVAR = "mean_var_norm_emb.ckpt"


def _model_dir(tmp_path: Path, *, hyperparams: bool = True) -> tuple[Path, dict[str, str]]:
    """Build a fake baked model dir; return it plus its true digests."""
    root = tmp_path / "ecapa"
    root.mkdir()
    (root / EMBEDDING).write_bytes(b"pretend-ecapa-weights" * 100)
    (root / MEANVAR).write_bytes(b"pretend-mean-var-norm")
    if hyperparams:
        (root / "hyperparams.yaml").write_text("pretrainer: {}\n", encoding="utf-8")
    pins = {
        EMBEDDING: sha256_file(root / EMBEDDING),
        MEANVAR: sha256_file(root / MEANVAR),
    }
    return root, pins


def test_matching_digests_verify_and_are_returned(tmp_path: Path) -> None:
    root, pins = _model_dir(tmp_path)
    actual = verify_model_dir(root, pins=pins, repo="speechbrain/x", revision="abc123")
    assert actual == pins


def test_tampered_artifact_refuses_to_start(tmp_path: Path) -> None:
    root, pins = _model_dir(tmp_path)
    # One flipped byte — the realistic corruption/tamper case.
    (root / EMBEDDING).write_bytes(b"pretend-ecapa-weights" * 100 + b"!")
    with pytest.raises(ModelIntegrityError, match="checksum mismatch"):
        verify_model_dir(root, pins=pins)


def test_truncated_artifact_refuses_to_start(tmp_path: Path) -> None:
    root, pins = _model_dir(tmp_path)
    (root / MEANVAR).write_bytes(b"pre")  # partial COPY / bad layer
    with pytest.raises(ModelIntegrityError, match="checksum mismatch"):
        verify_model_dir(root, pins=pins)


def test_missing_artifact_refuses_to_start(tmp_path: Path) -> None:
    root, pins = _model_dir(tmp_path)
    (root / EMBEDDING).unlink()
    with pytest.raises(ModelIntegrityError, match="missing diarization artifact"):
        verify_model_dir(root, pins=pins)


def test_missing_model_dir_refuses_to_start(tmp_path: Path) -> None:
    _, pins = _model_dir(tmp_path)
    with pytest.raises(ModelIntegrityError, match="model dir not found"):
        verify_model_dir(tmp_path / "nope", pins=pins)


def test_missing_patched_hyperparams_refuses_to_start(tmp_path: Path) -> None:
    """Without the repo-owned patched copy, SpeechBrain re-resolves the model
    over the network — the offline-hostile behaviour ADR-0034 rejected
    pyannote for. Absence must be fatal, not a warning."""
    root, pins = _model_dir(tmp_path, hyperparams=False)
    with pytest.raises(ModelIntegrityError, match="hyperparams.yaml"):
        verify_model_dir(root, pins=pins)


def test_unpinned_digest_verifies_presence_and_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The dev path (`make prepare-ecapa`, no digests configured) still runs,
    but says so loudly. Shipped images always set the digests via ENV."""
    root, _ = _model_dir(tmp_path)
    with caplog.at_level(logging.WARNING):
        actual = verify_model_dir(root, pins={EMBEDDING: "", MEANVAR: ""})
    assert set(actual) == {EMBEDDING, MEANVAR}
    assert any(r.message == "diarization.model_unpinned" for r in caplog.records)


def test_unpinned_still_requires_the_file_to_exist(tmp_path: Path) -> None:
    root, _ = _model_dir(tmp_path)
    (root / EMBEDDING).unlink()
    with pytest.raises(ModelIntegrityError, match="missing diarization artifact"):
        verify_model_dir(root, pins={EMBEDDING: "", MEANVAR: ""})


def test_sha256_file_matches_hashlib(tmp_path: Path) -> None:
    p = tmp_path / "blob"
    payload = b"x" * (1 << 21)  # spans the 1 MiB read chunks
    p.write_bytes(payload)
    assert sha256_file(p) == hashlib.sha256(payload).hexdigest()
