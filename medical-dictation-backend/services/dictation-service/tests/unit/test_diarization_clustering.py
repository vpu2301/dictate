"""Online 2-speaker clusterer tests (pure: synthetic unit vectors only).

Vector geometry: 7-dim unit vectors with exact cosines. Voice A lives
around axis e0; voice B around ``u = 0.2*e0 + sqrt(0.96)*e1`` (cosine
0.2 to e0). Per-voice chunks share the base direction with weight
``sqrt(0.95)`` and differ in a private perturbation axis with weight
``sqrt(0.05)``, giving intra-voice cosines of 0.90/0.95 and cross-voice
cosines of exactly 0.95 * 0.2 = 0.19 — comfortably on either side of
``split_threshold=0.45``.
"""

from __future__ import annotations

import math

import numpy as np

from dictation_service.diarization.clustering import (
    UNKNOWN,
    OnlineSpeakerClusterer,
)

DIM = 7
_C = math.sqrt(0.95)  # base-direction weight
_S = math.sqrt(0.05)  # perturbation weight


def _vec(base: np.ndarray, perturb_axis: int, sign: float = 1.0) -> np.ndarray:
    v = _C * base
    v[perturb_axis] += sign * _S
    return v.astype(np.float32)


def _base_a() -> np.ndarray:
    a = np.zeros(DIM)
    a[0] = 1.0
    return a


def _base_b() -> np.ndarray:
    b = np.zeros(DIM)
    b[0] = 0.2
    b[1] = math.sqrt(1.0 - 0.2 * 0.2)
    return b


def _voice_a() -> list[np.ndarray]:
    """Three voice-A vectors, mutual cosines 0.90/0.95."""
    a = _base_a()
    return [_vec(a.copy(), 2, +1.0), _vec(a.copy(), 2, -1.0), _vec(a.copy(), 3, +1.0)]


def _voice_b() -> list[np.ndarray]:
    """Three voice-B vectors, mutual cosines 0.90/0.95; cosine 0.19 to A."""
    b = _base_b()
    return [_vec(b.copy(), 4, +1.0), _vec(b.copy(), 4, -1.0), _vec(b.copy(), 5, +1.0)]


def _bootstrapped_clusterer() -> OnlineSpeakerClusterer:
    clusterer = OnlineSpeakerClusterer()
    for v in _voice_a() + _voice_b():
        clusterer.observe(v)
    assert clusterer.bootstrapped
    return clusterer


def test_single_voice_stays_s1_without_split() -> None:
    clusterer = OnlineSpeakerClusterer()
    a = _base_a()
    similar = [
        _vec(a.copy(), 2, +1.0),
        _vec(a.copy(), 2, -1.0),
        _vec(a.copy(), 3, +1.0),
        _vec(a.copy(), 3, -1.0),
    ]
    labels = []
    for v in similar:
        assignment, relabels = clusterer.observe(v)
        labels.append(assignment.label)
        assert relabels == []
    assert not clusterer.bootstrapped
    assert clusterer.speaker_count == 1
    assert labels == ["S1"] * 4


def test_split_lands_when_second_voice_accumulates() -> None:
    clusterer = OnlineSpeakerClusterer()
    all_relabels = []
    labels = []
    for v in _voice_a() + _voice_b():
        assignment, relabels = clusterer.observe(v)
        labels.append(assignment.label)
        all_relabels.extend(relabels)

    assert clusterer.bootstrapped
    assert clusterer.speaker_count == 2
    # Retrospective relabels: earlier voice-A chunks stay S1 (group with
    # chunk 0), the earlier voice-B chunk becomes S2.
    by_index = {r.chunk_index: r.label for r in all_relabels}
    assert by_index[0] == "S1"
    assert by_index[1] == "S1"
    assert by_index[2] == "S1"
    assert by_index[3] == "S2"
    # The chunk that triggered the split and the post-split chunk are S2.
    assert labels[4] == "S2"
    assert labels[5] == "S2"


def test_relabels_carry_chunk_indices_in_observation_order() -> None:
    clusterer = OnlineSpeakerClusterer()
    all_relabels = []
    for v in _voice_a() + _voice_b():
        _, relabels = clusterer.observe(v)
        all_relabels.extend(relabels)
    # Split lands on the 5th observation (index 4): every earlier chunk
    # is relabeled, in observation order.
    assert [r.chunk_index for r in all_relabels] == [0, 1, 2, 3]


def test_online_assignment_near_each_centroid() -> None:
    clusterer = _bootstrapped_clusterer()

    near_a = _base_a().astype(np.float32)
    assignment, relabels = clusterer.observe(near_a)
    assert relabels == []
    assert assignment.label == "S1"
    assert 0.0 < assignment.confidence <= 1.0

    near_b = _base_b().astype(np.float32)
    assignment, _ = clusterer.observe(near_b)
    assert assignment.label == "S2"
    assert 0.0 < assignment.confidence <= 1.0


def test_third_voice_below_assign_floor_is_unknown() -> None:
    clusterer = _bootstrapped_clusterer()
    third = np.zeros(DIM, dtype=np.float32)
    third[6] = 1.0  # orthogonal to both voices
    assignment, _ = clusterer.observe(third)
    assert assignment.label == UNKNOWN
    assert assignment.confidence == 0.0
    assert assignment.best_sim < clusterer.config.assign_floor


def test_equidistant_vector_is_unknown_by_ambiguity_margin() -> None:
    clusterer = _bootstrapped_clusterer()
    c1 = clusterer._centroids["S1"]
    c2 = clusterer._centroids["S2"]
    mid = c1 / np.linalg.norm(c1) + c2 / np.linalg.norm(c2)
    mid = (mid / np.linalg.norm(mid)).astype(np.float32)
    assignment, _ = clusterer.observe(mid)
    assert assignment.label == UNKNOWN
    assert assignment.confidence == 0.0
    # Above the floor but inside the ambiguity margin.
    assert assignment.best_sim >= clusterer.config.assign_floor
    assert assignment.best_sim - assignment.second_sim < clusterer.config.ambiguity_margin


def test_determinism_same_sequence_same_labels() -> None:
    sequence = _voice_a() + _voice_b() + [_base_a().astype(np.float32), _base_b().astype(np.float32)]
    runs = []
    for _ in range(2):
        clusterer = OnlineSpeakerClusterer()
        observed = []
        for v in sequence:
            assignment, relabels = clusterer.observe(v.copy())
            observed.append(
                (
                    assignment.label,
                    assignment.confidence,
                    [(r.chunk_index, r.label, r.confidence) for r in relabels],
                )
            )
        runs.append(observed)
    assert runs[0] == runs[1]
