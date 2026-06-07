"""Sequence-number gap policy tests."""

from __future__ import annotations

from dictation_service.audio.gap import (
    SAMPLES_PER_FRAME,
    GapDecision,
    GapPolicy,
    gap_decision,
)


def test_in_order_accept() -> None:
    r = gap_decision(expected_seq=5, incoming_seq=5)
    assert r.decision == GapDecision.ACCEPT
    assert r.next_expected_seq == 6


def test_duplicate_dropped() -> None:
    r = gap_decision(expected_seq=10, incoming_seq=7)
    assert r.decision == GapDecision.DUPLICATE
    assert r.next_expected_seq == 10


def test_small_gap_pads_silence() -> None:
    r = gap_decision(expected_seq=10, incoming_seq=30)
    assert r.decision == GapDecision.PAD_SILENCE
    assert r.pad_samples == 20 * SAMPLES_PER_FRAME
    assert r.next_expected_seq == 31


def test_small_gap_at_threshold() -> None:
    r = gap_decision(expected_seq=10, incoming_seq=60, policy=GapPolicy(small_gap_max_frames=50))
    assert r.decision == GapDecision.PAD_SILENCE


def test_large_gap_requests_retransmit() -> None:
    r = gap_decision(expected_seq=10, incoming_seq=65, policy=GapPolicy(small_gap_max_frames=50))
    assert r.decision == GapDecision.REQUEST_RETRANSMIT
    assert r.request_from_seq == 10
    assert r.next_expected_seq == 10
