"""Resume + retransmit policy tests (pure-logic; no DB)."""

from __future__ import annotations

from dictation_service.session.resume import evaluate_retransmit


def test_retransmit_zero_range_rejected() -> None:
    r = evaluate_retransmit(from_seq=10, to_seq=10, hwm=5)
    assert not r.accept
    assert not r.too_large


def test_retransmit_too_large_rejected() -> None:
    r = evaluate_retransmit(from_seq=0, to_seq=10_000, hwm=5)
    assert not r.accept
    assert r.too_large


def test_retransmit_partial_dedup() -> None:
    """from=5, to=15, hwm=10 → frames 5..10 are already received."""
    r = evaluate_retransmit(from_seq=5, to_seq=15, hwm=10)
    assert r.accept
    assert r.duped_seqs == 6  # 5, 6, 7, 8, 9, 10
