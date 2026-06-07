import fakeredis.aioredis
import pytest_asyncio


@pytest_asyncio.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=False)
    yield r
    await r.aclose()
