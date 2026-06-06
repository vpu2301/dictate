"""Cursor encode/decode tests."""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest

from report_service.domain.search import decode_cursor, encode_cursor


def test_roundtrip_with_date():
    rid = uuid4()
    d = date(2026, 5, 13)
    cur = encode_cursor(encounter_date=d, report_id=rid)
    out_d, out_id = decode_cursor(cur)
    assert out_d == d
    assert out_id == rid


def test_roundtrip_with_no_date():
    rid = uuid4()
    cur = encode_cursor(encounter_date=None, report_id=rid)
    out_d, out_id = decode_cursor(cur)
    assert out_d is None
    assert out_id == rid


def test_decode_corrupt_raises():
    with pytest.raises(Exception):
        decode_cursor("not_a_valid_cursor_!!!")
