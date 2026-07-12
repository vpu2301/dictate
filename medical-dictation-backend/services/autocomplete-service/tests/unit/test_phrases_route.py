"""Phrase/snippet write surface — PII rejection, rate limit, audit kinds.

Handlers exercised directly with a fake state (repo/limiter/DB faked):
the §8 unit matrix. RLS authority mapping is integration-tested.
"""

from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4

import pytest
from autocomplete_service import audit_kinds
from autocomplete_service.deps import install_state
from autocomplete_service.routers.phrases import (
    CreatePhraseRequest,
    CreateSnippetRequest,
    create_phrase,
)
from fastapi import HTTPException
from pydantic import ValidationError

from auth import Claims

pytestmark = pytest.mark.asyncio

REPO = Path(__file__).resolve().parents[4]


def _claims() -> Claims:
    return Claims(
        sub=uuid4(), tid=uuid4(), roles=["clinician"], scope="openid",
        sid="s", iss="test", aud="mdx-api", exp=2_000_000_000, iat=1,
    )


class _Audit:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def write_event(self, **kw):
        self.events.append(kw)


class _Metric:
    def __init__(self) -> None:
        self.added: list[dict] = []

    def add(self, n, attrs=None):
        self.added.append(attrs or {})


class _Limiter:
    def __init__(self, allowed=True) -> None:
        self.allowed = allowed

    async def check(self, *, user_id):
        return (True, 0) if self.allowed else (False, 1234)


class _NeverPool:
    """A pool that must never be touched (asserts nothing is persisted)."""

    def acquire(self):
        raise AssertionError("DB touched on a rejected write")


class _State:
    def __init__(self) -> None:
        self.audit_writer = _Audit()
        self.pii_rejections_metric = _Metric()
        self.phrase_rate_limiter = _Limiter()
        self.app_pool = _NeverPool()


# ── model-level validation ───────────────────────────────────────────────


def test_system_source_rejected_at_model_level():
    with pytest.raises(ValidationError):
        CreatePhraseRequest(phrase="x y", language="uk", source="system")


def test_snippet_trigger_format_enforced():
    # leading slash and non-latin triggers fail the DB-CHECK-mirroring pattern
    with pytest.raises(ValidationError):
        CreateSnippetRequest(trigger="/vit", expansion="x", language="uk")
    with pytest.raises(ValidationError):
        CreateSnippetRequest(trigger="мійтест", expansion="x", language="uk")
    CreateSnippetRequest(trigger="my_block-2", expansion="x", language="uk")


def test_snippet_cursor_beyond_expansion_rejected():
    with pytest.raises(ValidationError, match="within the expansion"):
        CreateSnippetRequest(trigger="vit", expansion="short", cursor_position=99, language="uk")


# ── PII rejection: 422 + sec audit + NOTHING persisted ───────────────────


async def test_pii_phrase_rejected_audited_never_stored():
    state = _State()
    install_state(state)
    body = CreatePhraseRequest(phrase="пацієнт ІПН 1234567890", language="uk")
    with pytest.raises(HTTPException) as exc:
        await create_phrase(body, _claims())
    assert exc.value.status_code == 422
    assert exc.value.detail["error"] == "pii_detected"
    # a 10-digit run hits both the exact-10 IPN class and the 7–14 phone sweep
    assert exc.value.detail["patterns"] == ["ipn", "phone"]
    # never echo the match
    assert "1234567890" not in str(exc.value.detail)
    # security audit emitted with length, not text
    (event,) = state.audit_writer.events
    assert event["kind"] == audit_kinds.PHRASE_WRITE_REJECTED_PII
    assert "1234567890" not in str(event["payload"])
    assert event["payload"]["text_length"] == len(body.phrase)
    # metric labelled by pattern (one add per class)
    assert state.pii_rejections_metric.added == [{"pattern": "ipn"}, {"pattern": "phone"}]
    # _NeverPool would have raised if the DB were touched


async def test_rate_limited_write_gets_429_before_anything_else():
    state = _State()
    state.phrase_rate_limiter = _Limiter(allowed=False)
    install_state(state)
    body = CreatePhraseRequest(phrase="звичайна фраза", language="uk")
    with pytest.raises(HTTPException) as exc:
        await create_phrase(body, _claims())
    assert exc.value.status_code == 429
    assert exc.value.detail["error"] == "rate_limited"
    assert exc.value.headers["Retry-After"] == "1234"
    assert state.audit_writer.events == []  # nothing audited, nothing stored


# ── audit-kind registration (grep-style, prior sprints' pattern) ─────────


def test_every_kind_in_code_is_registered_in_constants_and_docs():
    src_dir = REPO / "services" / "autocomplete-service" / "src"
    used = set()
    for p in src_dir.rglob("*.py"):
        used.update(re.findall(r"audit_kinds\.([A-Z_]+)", p.read_text("utf-8")))
    constants = {
        name for name in vars(audit_kinds) if name.isupper()
    }
    assert used <= constants, f"kinds used but not declared: {used - constants}"

    docs = (REPO / "docs" / "audit" / "event-kinds.md").read_text("utf-8")
    for name in constants:
        kind = getattr(audit_kinds, name)
        assert kind in docs, f"{kind} not registered in docs/audit/event-kinds.md"
