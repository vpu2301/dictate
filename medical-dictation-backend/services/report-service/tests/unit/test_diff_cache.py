"""DiffCache unit tests."""

from __future__ import annotations

from uuid import uuid4

from report_models import DiffResponse, MetadataDiff
from report_service.domain.diff_cache import DiffCache


def _payload(report_id, from_id, to_id):
    return DiffResponse(
        report_id=str(report_id),
        from_version_id=str(from_id),
        from_version_number=1,
        to_version_id=str(to_id),
        to_version_number=2,
        sections=[],
        metadata=MetadataDiff(),
    )


def test_put_get_hit_increments():
    c = DiffCache(max_entries=4)
    rid, fid, tid = uuid4(), uuid4(), uuid4()
    p = _payload(rid, fid, tid)
    c.put(report_id=rid, from_id=fid, to_id=tid, value=p)
    hit = c.get(report_id=rid, from_id=fid, to_id=tid)
    assert hit is not None
    assert hit.cached is True
    assert c.hits == 1


def test_miss_increments():
    c = DiffCache()
    c.get(report_id=uuid4(), from_id=uuid4(), to_id=uuid4())
    assert c.misses == 1
    assert c.hit_ratio == 0.0


def test_lru_evicts_oldest():
    c = DiffCache(max_entries=2)
    rid = uuid4()
    pairs = [(uuid4(), uuid4()) for _ in range(3)]
    for a, b in pairs:
        c.put(report_id=rid, from_id=a, to_id=b, value=_payload(rid, a, b))
    # First inserted should have been evicted.
    assert c.get(report_id=rid, from_id=pairs[0][0], to_id=pairs[0][1]) is None
    assert c.get(report_id=rid, from_id=pairs[2][0], to_id=pairs[2][1]) is not None
