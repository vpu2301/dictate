"""ECAPA-TDNN speaker-embedding wrapper (sprint 14, ADR-0034).

Loads the pinned model dir produced by ``scripts/models/prepare_ecapa.py``
(baked at ``/opt/models/ecapa`` in prod images — docs/models/PINS.md) fully
offline and exposes a single ``embed()`` call returning an L2-normalised
192-dim vector. One instance per process, shared by every conversation
session (the model is stateless; ~90 MB resident once).
"""

from __future__ import annotations

from typing import Any

import numpy as np

SAMPLE_RATE_HZ = 16_000


class EcapaEmbedder:
    def __init__(self, *, model_dir: str, device: str = "cpu") -> None:
        self._model_dir = model_dir
        self._device = device
        self._clf: Any = None

    @property
    def device(self) -> str:
        return self._device

    def _ensure_loaded(self) -> None:
        if self._clf is not None:
            return
        import torch
        from speechbrain.inference.speaker import EncoderClassifier

        torch.set_grad_enabled(False)
        # source == savedir: everything is already in the baked dir; no
        # fetch, no symlink farm, no network (HF_HUB_OFFLINE-safe).
        self._clf = EncoderClassifier.from_hparams(
            source=self._model_dir,
            savedir=self._model_dir,
            run_opts={"device": self._device},
        )

    def warm_up(self) -> None:
        """Load weights and run one dummy forward so the first real
        window doesn't pay the initialisation cost."""
        self._ensure_loaded()
        self.embed(np.zeros(SAMPLE_RATE_HZ // 2, dtype=np.float32))

    def embed(self, pcm: np.ndarray) -> np.ndarray:
        """float32 mono 16 kHz PCM -> unit-norm (192,) float32 vector."""
        self._ensure_loaded()
        import torch

        signal = torch.from_numpy(
            np.ascontiguousarray(pcm, dtype=np.float32)
        ).unsqueeze(0)
        with torch.no_grad():
            emb = self._clf.encode_batch(signal).squeeze()
        vec: np.ndarray = np.asarray(
            emb.detach().cpu().numpy(), dtype=np.float32
        ).reshape(-1)
        norm = float(np.linalg.norm(vec))
        if norm > 0.0:
            vec = vec / norm
        return vec
