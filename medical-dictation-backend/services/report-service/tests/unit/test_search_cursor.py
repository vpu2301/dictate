"""Cursor encode/decode tests."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from report_service.domain.search import decode_cursor, encode_cursor


def test_roundtrip():
    rid = uuid4()
    c = datetime(2026, 5, 13, 14, 30, 15, tzinfo=UTC)
    cur = encode_cursor(created_at=c, report_id=rid)
    out_c, out_id = decode_cursor(cur)
    assert out_c == c
    assert out_id == rid


def test_roundtrip_preserves_microseconds():
    rid = uuid4()
    c = datetime(2026, 7, 1, 21, 38, 4, 811835, tzinfo=UTC)
    cur = encode_cursor(created_at=c, report_id=rid)
    out_c, out_id = decode_cursor(cur)
    assert out_c == c
    assert out_id == rid


def test_decode_corrupt_raises():
    with pytest.raises(ValueError):
        decode_cursor("not_a_valid_cursor_!!!")
