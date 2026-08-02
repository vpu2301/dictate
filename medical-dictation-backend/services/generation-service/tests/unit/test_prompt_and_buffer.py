"""Prompt builder + aggregated shown-audit buffer."""

from __future__ import annotations

from uuid import uuid4

from generation_service.domain.prompt import build_prompt
from generation_service.domain.shown_audit import ShownAuditBuffer


def test_prompt_uk_contains_frame_section_and_text():
    prompt = build_prompt(
        section_key="anamnesis",
        text_before_cursor="Пацієнт скаржиться на",
        language="uk",
    )
    assert "Не вигадуй жодних нових клінічних фактів" in prompt
    assert "Розділ документа: anamnesis" in prompt
    assert prompt.endswith("Пацієнт скаржиться на")


def test_prompt_en_variant():
    prompt = build_prompt(
        section_key="objective", text_before_cursor="The patient reports", language="en"
    )
    assert "Do not invent any new clinical facts" in prompt
    assert "Document section: objective" in prompt


async def test_shown_buffer_aggregates_per_tenant():
    flushed: list[tuple] = []

    async def _flush(tenant_id, count):
        flushed.append((tenant_id, count))

    buf = ShownAuditBuffer(flush_fn=_flush, flush_interval_s=3600)
    t1, t2 = uuid4(), uuid4()
    for _ in range(3):
        await buf.record(tenant_id=t1)
    await buf.record(tenant_id=t2)
    await buf.flush_all()
    assert sorted(flushed, key=lambda x: x[1]) == [(t2, 1), (t1, 3)]
    # Second flush is a no-op — counts were cleared.
    await buf.flush_all()
    assert len(flushed) == 2


async def test_shown_buffer_flush_error_swallowed():
    async def _flush(tenant_id, count):
        raise RuntimeError("audit down")

    buf = ShownAuditBuffer(flush_fn=_flush, flush_interval_s=3600)
    await buf.record(tenant_id=uuid4())
    await buf.flush_all()  # must not raise
