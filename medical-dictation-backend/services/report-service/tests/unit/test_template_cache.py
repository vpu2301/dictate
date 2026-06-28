"""TemplateCache unit tests (sprint 06).

The cache is pure in-process logic — no DB. We verify the tenant-scoped
key, hit/miss accounting, and invalidation that PUT/DELETE rely on.
"""

from __future__ import annotations

from uuid import uuid4

from report_service.domain.cache import CachedTemplate, TemplateCache


def _cached(template_id, tenant_id) -> CachedTemplate:
    return CachedTemplate(
        template_id=template_id,
        tenant_id=tenant_id,
        schema_jsonb={"code": "x", "sections": []},
        schema_version=1,
        status="active",
    )


def test_miss_then_hit() -> None:
    cache = TemplateCache()
    tenant = uuid4()
    tid = uuid4()

    assert cache.get(tenant_id=tenant, template_id=tid) is None  # miss
    cache.put(tenant_id=tenant, template_id=tid, cached=_cached(tid, tenant))
    got = cache.get(tenant_id=tenant, template_id=tid)  # hit
    assert got is not None
    assert got.template_id == tid
    assert cache.hit_ratio == 0.5  # 1 hit / 2 calls


def test_key_is_tenant_scoped() -> None:
    """Same template_id under a different tenant must not collide."""
    cache = TemplateCache()
    tenant_a, tenant_b = uuid4(), uuid4()
    tid = uuid4()

    cache.put(tenant_id=tenant_a, template_id=tid, cached=_cached(tid, tenant_a))
    assert cache.get(tenant_id=tenant_a, template_id=tid) is not None
    assert cache.get(tenant_id=tenant_b, template_id=tid) is None


def test_invalidate_clears_key() -> None:
    cache = TemplateCache()
    tenant = uuid4()
    tid = uuid4()

    cache.put(tenant_id=tenant, template_id=tid, cached=_cached(tid, tenant))
    cache.invalidate(tenant_id=tenant, template_id=tid)
    assert cache.get(tenant_id=tenant, template_id=tid) is None


def test_invalidate_missing_key_is_noop() -> None:
    cache = TemplateCache()
    # Must not raise even if the key was never cached.
    cache.invalidate(tenant_id=uuid4(), template_id=uuid4())
