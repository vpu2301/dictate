"""Online 2-speaker clustering with a deterministic bootstrap (ADR-0034).

Streaming diarizers fail when the second speaker slot is seeded from a
noisy same-speaker chunk — the real second voice then never gets a slot.
So clustering runs in two phases:

**Bootstrap** (until two confirmed speakers): every chunk is provisionally
S1/pending; after each observation a complete-linkage 2-way split over all
embeddings so far is attempted. The split is accepted only when the two
groups are mutually distant (max cross-group sim < ``split_threshold``)
and each side has ``min_split_mass`` members — noise cannot fabricate a
speaker. On acceptance, all prior chunks are retrospectively relabeled
(the stream keeps chunk→segment indices for exactly this).

**Online** (both slots taken): nearest-centroid with an ambiguity margin
and an assignment floor. A third voice lands below the floor → UNKNOWN,
never a guess (pilot cap: 2 speakers, documented limitation).

Everything is deterministic in stream order; no randomness anywhere.

Confidence formula (documented in docs/api/dictation-ws-v2.md):

    conf = 0.5 * best_sim + 0.5 * min(1, (best_sim - second_sim) / margin_scale)
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ClusteringConfig:
    max_speakers: int = 2
    # Bootstrap split: mean cross-group cosine must fall below this AND
    # trail mean intra-group cosine by at least ``split_min_gap`` for a
    # 2-way split to be accepted (fixture calibration: same-voice chunk
    # sims ≥ ~0.5, cross-voice ≤ ~0.45). Mean-based on purpose: a single
    # noisy mixed-speaker chunk must not veto the split forever.
    split_threshold: float = 0.45
    split_min_gap: float = 0.12
    min_split_mass: int = 2
    # Keep at most this many embeddings for the bootstrap evaluation.
    bootstrap_max_chunks: int = 120
    # Online phase:
    assign_floor: float = 0.45  # below vs every centroid -> UNKNOWN
    ambiguity_margin: float = 0.08  # best-vs-runner-up closer than this -> UNKNOWN
    centroid_update_min_sim: float = 0.60
    margin_scale: float = 0.30


@dataclass(frozen=True)
class Assignment:
    label: str  # "S1" | "S2" | UNKNOWN
    confidence: float
    best_sim: float
    second_sim: float


@dataclass(frozen=True)
class Relabel:
    """Retrospective correction emitted when the bootstrap split lands."""

    chunk_index: int
    label: str
    confidence: float


class OnlineSpeakerClusterer:
    def __init__(self, config: ClusteringConfig | None = None) -> None:
        self.config = config or ClusteringConfig()
        self._embeddings: list[np.ndarray] = []  # bootstrap evidence, capped
        self._centroids: dict[str, np.ndarray] = {}
        self._counts: dict[str, int] = {}
        self._bootstrapped = False

    @property
    def speaker_count(self) -> int:
        return len(self._centroids)

    @property
    def bootstrapped(self) -> bool:
        return self._bootstrapped

    # ── main entry ────────────────────────────────────────────────────
    def observe(self, embedding: np.ndarray) -> tuple[Assignment, list[Relabel]]:
        """Returns (assignment for this chunk, retrospective relabels)."""
        if self._bootstrapped:
            return self._assign_online(embedding), []
        return self._observe_bootstrap(embedding)

    # ── bootstrap phase ───────────────────────────────────────────────
    def _observe_bootstrap(self, embedding: np.ndarray) -> tuple[Assignment, list[Relabel]]:
        cfg = self.config
        if len(self._embeddings) < cfg.bootstrap_max_chunks:
            self._embeddings.append(embedding.copy())
        idx = len(self._embeddings) - 1

        groups = _complete_linkage_split(
            self._embeddings, cfg.split_threshold, cfg.split_min_gap
        )
        if groups is not None and all(len(g) >= cfg.min_split_mass for g in groups):
            # Accepted: group containing the FIRST chunk is S1 (stream order).
            first_group = 0 if 0 in groups[0] else 1
            labels = {"S1": list(groups[first_group]), "S2": list(groups[1 - first_group])}
            for label, members in labels.items():
                self._centroids[label] = np.mean([self._embeddings[i] for i in members], axis=0)
                self._counts[label] = len(members)

            # One-level sub-split: a third voice similar to one speaker
            # gets absorbed into that speaker's group and would poison
            # its centroid. If a group internally splits, keep the
            # sub-group BETTER separated from the other speaker as the
            # participant (maximises inter-speaker separation); the
            # rejected sub-group is left to the re-scoring below, where
            # near-equidistance lands it UNKNOWN.
            for label in ("S1", "S2"):
                group = labels[label]
                if len(group) < 2 * cfg.min_split_mass:
                    continue
                sub = _complete_linkage_split(
                    [self._embeddings[i] for i in group],
                    cfg.split_threshold,
                    cfg.split_min_gap,
                )
                if sub is None:
                    continue
                other_centroid = self._centroids["S2" if label == "S1" else "S1"]
                # Sub-group indices are positions WITHIN `group`.
                sim_to_other = [
                    self._mean_similarity([group[i] for i in sub_idx], other_centroid)
                    for sub_idx in sub
                ]
                keep_idx = 0 if sim_to_other[0] <= sim_to_other[1] else 1
                keep = [group[i] for i in sub[keep_idx]]
                if len(keep) < cfg.min_split_mass:
                    continue
                labels[label] = keep
                self._centroids[label] = np.mean([self._embeddings[i] for i in keep], axis=0)
                self._counts[label] = len(keep)

            # Re-score EVERY chunk with the online rule. A third voice
            # that slipped into the pool during bootstrap sits nearly
            # equidistant from both centroids and lands UNKNOWN here —
            # the split itself cannot express a third speaker. Then
            # refine centroids from confident members only (one pass;
            # a mixed-in third voice must not poison a centroid) and
            # score once more.
            for _pass in range(2):
                scored = [self._assign_online_readonly(e) for e in self._embeddings]
                for label in ("S1", "S2"):
                    confident = [
                        self._embeddings[i]
                        for i, a in enumerate(scored)
                        if a.label == label and a.best_sim >= cfg.centroid_update_min_sim
                    ]
                    if confident:
                        self._centroids[label] = np.mean(confident, axis=0)
                        self._counts[label] = len(confident)
            scored = [self._assign_online_readonly(e) for e in self._embeddings]

            relabels = [
                Relabel(i, a.label, a.confidence) for i, a in enumerate(scored) if i != idx
            ]
            self._bootstrapped = True
            self._embeddings.clear()
            return scored[idx], relabels

        # No split yet: single provisional speaker. Confidence stays low —
        # attribution treats pre-bootstrap audio as pending, so these
        # labels never reach the wire prematurely.
        if "S1" not in self._centroids:
            self._centroids["S1"] = embedding.copy()
            self._counts["S1"] = 1
            return Assignment("S1", 0.0, 1.0, 0.0), []
        best = _cos(self._centroids["S1"], embedding)
        n = self._counts["S1"]
        self._centroids["S1"] = (self._centroids["S1"] * n + embedding) / (n + 1)
        self._counts["S1"] = n + 1
        # Confidence is meaningful only if this really is a one-voice
        # session; the stream's attribution gate holds these back until
        # either the split lands (relabel) or the single-speaker regime
        # is established.
        return Assignment("S1", _confidence(best, 0.0, cfg.margin_scale), best, 0.0), []

    def _mean_similarity(self, indices: list[int], centroid: np.ndarray) -> float:
        if not indices:
            return 0.0
        return sum(_cos(self._embeddings[i], centroid) for i in indices) / len(indices)

    def _assign_online_readonly(self, embedding: np.ndarray) -> Assignment:
        """Online scoring without centroid updates (bootstrap re-scoring)."""
        cfg = self.config
        sims = {label: _cos(c, embedding) for label, c in self._centroids.items()}
        ordered = sorted(sims, key=lambda k: sims[k], reverse=True)
        best_label = ordered[0]
        best = sims[best_label]
        second = sims[ordered[1]] if len(ordered) > 1 else 0.0
        if best < cfg.assign_floor:
            return Assignment(UNKNOWN, 0.0, best, second)
        if len(ordered) > 1 and (best - second) < cfg.ambiguity_margin:
            return Assignment(UNKNOWN, 0.0, best, second)
        return Assignment(best_label, _confidence(best, second, cfg.margin_scale), best, second)

    # ── online phase ──────────────────────────────────────────────────
    def _assign_online(self, embedding: np.ndarray) -> Assignment:
        cfg = self.config
        sims = {label: _cos(c, embedding) for label, c in self._centroids.items()}
        ordered = sorted(sims, key=lambda k: sims[k], reverse=True)
        best_label = ordered[0]
        best = sims[best_label]
        second = sims[ordered[1]] if len(ordered) > 1 else 0.0

        if best < cfg.assign_floor:
            return Assignment(UNKNOWN, 0.0, best, second)
        if len(ordered) > 1 and (best - second) < cfg.ambiguity_margin:
            return Assignment(UNKNOWN, 0.0, best, second)

        if best >= cfg.centroid_update_min_sim:
            n = self._counts[best_label]
            self._centroids[best_label] = (self._centroids[best_label] * n + embedding) / (n + 1)
            self._counts[best_label] = n + 1
        return Assignment(best_label, _confidence(best, second, cfg.margin_scale), best, second)


def _cos(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def _confidence(best: float, second: float, margin_scale: float) -> float:
    conf = 0.5 * best + 0.5 * min(1.0, max(0.0, best - second) / margin_scale)
    return float(max(0.0, min(1.0, round(conf, 4))))


def _complete_linkage_split(
    embeddings: list[np.ndarray], split_threshold: float, split_min_gap: float
) -> tuple[list[int], list[int]] | None:
    """Agglomerative complete-linkage until 2 clusters remain; accept the
    split iff mean cross-group sim < ``split_threshold`` and the intra/
    cross separation gap ≥ ``split_min_gap``. Deterministic: ties broken
    by lowest index (stream order)."""
    n = len(embeddings)
    if n < 2:
        return None
    clusters: list[list[int]] = [[i] for i in range(n)]

    def link(a: list[int], b: list[int]) -> float:
        # complete linkage on cosine similarity = MIN similarity across pairs
        return min(_cos(embeddings[i], embeddings[j]) for i in a for j in b)

    while len(clusters) > 2:
        best_pair, best_sim = None, -2.0
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                s = link(clusters[i], clusters[j])
                if s > best_sim + 1e-12:
                    best_sim, best_pair = s, (i, j)
        assert best_pair is not None
        i, j = best_pair
        clusters[i] = clusters[i] + clusters[j]
        del clusters[j]

    a, b = clusters
    cross_sims = [_cos(embeddings[i], embeddings[j]) for i in a for j in b]
    cross_mean = sum(cross_sims) / len(cross_sims)
    intra_sims = [
        _cos(embeddings[i], embeddings[j])
        for grp in (a, b)
        for k, i in enumerate(grp)
        for j in grp[k + 1 :]
    ]
    intra_mean = sum(intra_sims) / len(intra_sims) if intra_sims else 1.0
    if cross_mean >= split_threshold or (intra_mean - cross_mean) < split_min_gap:
        return None
    return a, b
