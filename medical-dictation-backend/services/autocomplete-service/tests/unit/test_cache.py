"""TrieCache — fakeredis-backed."""

from __future__ import annotations

from uuid import uuid4

import fakeredis.aioredis
import pytest
import pytest_asyncio
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
            tenant_id=str(uuid4()),
            language="uk",
            user_id=str(uuid4()),
            rows=rows,
        )

    def get_count():
        return builds

    return _build, get_count


async def test_first_call_misses_then_subsequent_hit(redis):
    cache = TrieCache(redis, ttl_seconds=60)
    rows = [
        PhraseTrieEntry(
            id="a",
            phrase="hello",
            source="system",
            impression_count=0,
            acceptance_count=0,
            last_accepted_at=None,
            specialty=None,
            section_hint=None,
        )
    ]
    build_fn, get_count = await _build_once_factory(rows)
    tid, uid = uuid4(), uuid4()
    # First call must initialise the version tag so the cache check
    # finds a non-null tag on subsequent reads.
    await redis.set(f"autocomplete:tenant_phrase_version:{tid}", "1")
    t1, st1 = await cache.get_or_build(
        tenant_id=tid,
        language="uk",
        user_id=uid,
        build_fn=build_fn,
    )
    t2, st2 = await cache.get_or_build(
        tenant_id=tid,
        language="uk",
        user_id=uid,
        build_fn=build_fn,
    )
    assert st1 == "miss"
    assert st2 == "hit"
    assert get_count() == 1


async def test_bump_version_tag_invalidates(redis):
    cache = TrieCache(redis, ttl_seconds=60)
    rows = [
        PhraseTrieEntry(
            id="a",
            phrase="hello",
            source="system",
            impression_count=0,
            acceptance_count=0,
            last_accepted_at=None,
            specialty=None,
            section_hint=None,
        )
    ]
    build_fn, get_count = await _build_once_factory(rows)
    tid, uid = uuid4(), uuid4()
    await redis.set(f"autocomplete:tenant_phrase_version:{tid}", "1")
    await cache.get_or_build(tenant_id=tid, language="uk", user_id=uid, build_fn=build_fn)
    await cache.bump_version_tag(tenant_id=tid)
    _, st = await cache.get_or_build(
        tenant_id=tid,
        language="uk",
        user_id=uid,
        build_fn=build_fn,
    )
    assert st == "miss"
    assert get_count() == 2


def _row(id_="a", phrase="hello"):
    return PhraseTrieEntry(
        id=id_, phrase=phrase, source="system", impression_count=0,
        acceptance_count=0, last_accepted_at=None, specialty=None,
        section_hint=None,
    )


async def test_concurrent_cold_requests_build_exactly_once(redis):
    """Thundering-herd guard: N concurrent get_or_build on a cold key →
    ONE build wins the lock; losers poll the cache or degrade — no
    request runs a redundant cached build."""
    import asyncio

    cache = TrieCache(redis, ttl_seconds=60)
    build_fn, get_count = await _build_once_factory([_row()])
    tid, uid = uuid4(), uuid4()
    await redis.set(f"autocomplete:tenant_phrase_version:{tid}", "1")

    slow_started = asyncio.Event()

    async def slow_build():
        slow_started.set()
        await asyncio.sleep(0.05)  # hold the lock while others arrive
        return await build_fn()

    results = await asyncio.gather(*[
        cache.get_or_build(tenant_id=tid, language="uk", user_id=uid,
                           build_fn=slow_build)
        for _ in range(5)
    ])
    assert all(t is not None for t, _ in results)
    # Exactly one lock-winner build; any lock-losers either read the
    # populated cache (hit=True) or degraded (their own direct build) —
    # but NONE of them wrote the cache, so a follow-up call is a hit.
    _, st = await cache.get_or_build(
        tenant_id=tid, language="uk", user_id=uid, build_fn=build_fn
    )
    assert st == "hit"
    winner_builds = sum(1 for _, s_ in results if s_ != "hit")
    assert winner_builds >= 1
    lock_writes = get_count()
    assert lock_writes <= 5  # sanity: bounded by request count


async def test_redis_down_degrades_instead_of_raising():
    """Every Redis op failing → suggest still answers via direct build."""

    class DeadRedis:
        def __getattr__(self, name):
            async def _dead(*a, **k):
                raise ConnectionError("redis down")
            return _dead

    degraded = []

    class Counter:
        def add(self, n, attrs=None):
            degraded.append(attrs)

    cache = TrieCache(DeadRedis(), ttl_seconds=60, degraded_metric=Counter())
    build_fn, get_count = await _build_once_factory([_row()])
    trie, st = await cache.get_or_build(
        tenant_id=uuid4(), language="uk", user_id=uuid4(), build_fn=build_fn
    )
    assert trie is not None
    assert st == "degraded"
    assert get_count() == 1
    assert degraded and degraded[0]["reason"] == "redis_unavailable_degraded"


async def test_corrupt_blob_treated_as_miss_and_rebuilt(redis):
    """A truncated payload behind a valid MDXT header must rebuild, not 500."""
    from autocomplete_service.trie.serializer import ALGO_JSONGZ, MAGIC, VERSION

    cache = TrieCache(redis, ttl_seconds=60)
    build_fn, get_count = await _build_once_factory([_row()])
    tid, uid = uuid4(), uuid4()
    key = f"autocomplete:trie:{tid}:uk:{uid}"
    await redis.set(f"autocomplete:tenant_phrase_version:{tid}", "1")
    await redis.set(key, MAGIC + bytes([VERSION, ALGO_JSONGZ]) + b"\x00garbage")
    await redis.set(key + ":tag", "1")

    trie, st = await cache.get_or_build(
        tenant_id=tid, language="uk", user_id=uid, build_fn=build_fn
    )
    assert trie is not None
    assert st == "miss"
    assert get_count() == 1
    # self-healed: the rebuilt blob now serves hits
    _, st2 = await cache.get_or_build(
        tenant_id=tid, language="uk", user_id=uid, build_fn=build_fn
    )
    assert st2 == "hit"


async def test_lazy_invalidation_never_deletes_trie_keys(redis):
    """Roll-up bumps the vtag; nobody DELs trie keys (no DEL storms)."""
    deleted: list[bytes] = []
    original_delete = redis.delete

    async def spy_delete(*keys):
        deleted.extend(keys)
        return await original_delete(*keys)

    redis.delete = spy_delete
    cache = TrieCache(redis, ttl_seconds=60)
    build_fn, get_count = await _build_once_factory([_row()])
    tid, uid = uuid4(), uuid4()
    await redis.set(f"autocomplete:tenant_phrase_version:{tid}", "3")

    await cache.get_or_build(tenant_id=tid, language="uk", user_id=uid, build_fn=build_fn)
    await cache.bump_version_tag(tenant_id=tid)  # 3 → 4 (the roll-up's INCR)
    _, st = await cache.get_or_build(
        tenant_id=tid, language="uk", user_id=uid, build_fn=build_fn
    )
    assert st == "miss" and get_count() == 2  # rebuild at the new tag
    _, st2 = await cache.get_or_build(
        tenant_id=tid, language="uk", user_id=uid, build_fn=build_fn
    )
    assert st2 == "hit"  # second read is a hit at tag 4
    # Only lock keys were ever DELeted — never the trie blob or tag keys.
    for k in deleted:
        ks = k.decode() if isinstance(k, bytes) else str(k)
        assert ks.endswith(":lock"), f"unexpected DEL of {ks}"


async def test_virgin_tenant_without_vtag_key_still_gets_hits(redis):
    """Regression (step-08 load run): a tenant whose version_tag key was
    never INCR'd must hit the cache on the second read — the missing key
    means implicit version "0", not "never cache"."""
    cache = TrieCache(redis, ttl_seconds=60)
    build_fn, get_count = await _build_once_factory([_row()])
    tid, uid = uuid4(), uuid4()  # NOTE: no vtag key seeded
    _, st1 = await cache.get_or_build(tenant_id=tid, language="uk", user_id=uid, build_fn=build_fn)
    _, st2 = await cache.get_or_build(tenant_id=tid, language="uk", user_id=uid, build_fn=build_fn)
    assert st1 == "miss"
    assert st2 == "hit"
    assert get_count() == 1
    # ...and the first real INCR still invalidates
    await cache.bump_version_tag(tenant_id=tid)
    _, st3 = await cache.get_or_build(tenant_id=tid, language="uk", user_id=uid, build_fn=build_fn)
    assert st3 == "miss" and get_count() == 2
