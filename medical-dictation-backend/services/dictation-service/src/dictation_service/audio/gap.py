"""Sequence-number gap policy.

The client sends binary frames with a 4-byte BE sequence number. Network
reorderings, duplicates, and small drops are normal. The policy:

- ``seq < expected`` → duplicate. Drop.
- ``seq == expected`` → in order. Accept.
- ``seq > expected`` and gap ≤ 50 frames (1 s) → fill with silence
  padding; accept.
- ``seq > expected`` and gap > 50 frames → server asks the client to
  retransmit from ``expected``.

The thresholds live in the policy struct so chaos tests can shrink them
without re-flowing through ``settings``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

SAMPLES_PER_FRAME: Final = 320  # 20 ms @ 16 kHz


class GapDecision(StrEnum):
    ACCEPT = "accept"  # in order
    DUPLICATE = "duplicate"  # drop silently
    PAD_SILENCE = "pad_silence"  # small gap, fill
    REQUEST_RETRANSMIT = "request_retransmit"  # big gap


@dataclass(frozen=True, slots=True)
class GapPolicy:
    small_gap_max_frames: int = 50  # 1 s at 50 fps


_DEFAULT_GAP_POLICY = GapPolicy()


@dataclass(frozen=True, slots=True)
class GapResult:
    decision: GapDecision
    pad_samples: int = 0
    request_from_seq: int = 0
    next_expected_seq: int = 0


def gap_decision(
    expected_seq: int,
    incoming_seq: int,
    *,
    policy: GapPolicy = _DEFAULT_GAP_POLICY,
) -> GapResult:
    if incoming_seq < expected_seq:
        return GapResult(
            decision=GapDecision.DUPLICATE,
            next_expected_seq=expected_seq,
        )
    if incoming_seq == expected_seq:
        return GapResult(
            decision=GapDecision.ACCEPT,
            next_expected_seq=expected_seq + 1,
        )
    gap = incoming_seq - expected_seq
    if gap <= policy.small_gap_max_frames:
        return GapResult(
            decision=GapDecision.PAD_SILENCE,
            pad_samples=gap * SAMPLES_PER_FRAME,
            next_expected_seq=incoming_seq + 1,
        )
    return GapResult(
        decision=GapDecision.REQUEST_RETRANSMIT,
        request_from_seq=expected_seq,
        next_expected_seq=expected_seq,  # don't advance until retransmit
    )
