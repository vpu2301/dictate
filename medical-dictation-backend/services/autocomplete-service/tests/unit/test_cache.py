"""TrieCache — fakeredis-backed."""

from __future__ import annotations

import pytest
import pytest_asyncio
import fakeredis.aioredis
from uuid import uuid4

from autocomplete_service.trie import build_trie_from_phrases
from autocomplete_service.trie.builder import PhraseTrieEntry
from autocomplete_service.trie.cache import TrieCache


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=False)
    yield r
    await r.aclose()


async def _build_once_factory(rows: list[PhraseTrieEntry]):
    builds = 0

    async def _build():
        nonlocal builds
        builds += 1
        return build_trie_from_phrases(
            tenant_id=str(uuid4()), language="uk", user_id=str(uuid4()), rows=rows,
        )

    def get_count(): return builds
    return _build, get_count


async def test_first_call_misses_then_subsequent_hit(redis):
    cache = TrieCache(redis, ttl_seconds=60)
    rows = [PhraseTrieEntry(id="a", phrase="hello", source="system",
                            impression_count=0, acceptance_count=0,
                            last_accepted_at=None, specialty=None, section_hint=None)]
    build_fn, get_count = await _build_once_factory(rows)
    tid, uid = uuid4(), uuid4()
    # First call must initialise the version tag so the cache check
    # finds a non-null tag on subsequent reads.
    await redis.set(f"autocomplete:tenant_phrase_version:{tid}", "1")
    t1, hit1 = await cache.get_or_build(
        tenant_id=tid, language="uk", user_id=uid, build_fn=build_fn,
    )
    t2, hit2 = await cache.get_or_build(
        tenant_id=tid, language="uk", user_id=uid, build_fn=build_fn,
    )
    assert hit1 is False
    assert hit2 is True
    assert get_count() == 1


async def test_bump_version_tag_invalidates(redis):
    cache = TrieCache(redis, ttl_seconds=60)
    rows = [PhraseTrieEntry(id="a", phrase="hello", source="system",
                            impression_count=0, acceptance_count=0,
                            last_accepted_at=None, specialty=None, section_hint=None)]
    build_fn, get_count = await _build_once_factory(rows)
    tid, uid = uuid4(), uuid4()
    await redis.set(f"autocomplete:tenant_phrase_version:{tid}", "1")
    await cache.get_or_build(tenant_id=tid, language="uk", user_id=uid, build_fn=build_fn)
    await cache.bump_version_tag(tenant_id=tid)
    _, hit = await cache.get_or_build(
        tenant_id=tid, language="uk", user_id=uid, build_fn=build_fn,
    )
    assert hit is False
    assert get_count() == 2
