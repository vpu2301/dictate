"""S11 step 08 — closeout guards: scrub parity, kind registration,
metric-label discipline.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]


# ── Scrub parity: one DPO-reviewed pattern family, two copies ───────


def test_scrub_patterns_are_verbatim_s10_copies() -> None:
    """core_service.scrub must carry the EXACT S10 scrubber patterns
    (services can't import services, so it's a pinned copy — any change
    to either side is a DPO re-review and must land in both)."""
    from autocomplete_service import scrubber

    from core_service import scrub

    assert [(n, p.pattern) for n, p in scrub._PATTERNS] == [
        (n, p.pattern) for n, p in scrubber._PATTERNS
    ]


def test_scrub_redacts_the_shapes() -> None:
    from core_service.scrub import scrub_free_text

    dirty = (
        "пацієнт 1759013776, тел +380501234567, email x@example.com, "
        "нар. 01.02.1980 — дублікат"
    )
    clean = scrub_free_text(dirty)
    for leak in ("1759013776", "+380501234567", "x@example.com", "01.02.1980"):
        assert leak not in clean
    assert "дублікат" in clean  # ordinary text survives


def test_rejection_reason_is_scrubbed_into_audit(monkeypatch) -> None:
    """The one boundary-crossing free-text path found by the gap check."""
    import inspect

    from core_service.routers import privacy

    src = inspect.getsource(privacy)
    assert "scrub_free_text(body.rejection_reason" in src


# ── Kind registration: every emitted kind is documented ─────────────


def test_every_core_service_audit_kind_is_documented() -> None:
    import core_service.audit_kinds as kinds_mod

    doc = (REPO / "docs" / "audit" / "event-kinds.md").read_text("utf-8")
    constants = {
        v for k, v in vars(kinds_mod).items()
        if k.isupper() and isinstance(v, str)
    }
    missing = {k for k in constants if f"`{k}`" not in doc}
    assert not missing, f"emitted kinds missing from event-kinds.md: {missing}"


def test_no_undocumented_stringly_kinds_in_core_service() -> None:
    """Any dotted kind literal (the audit-kind shape) hardcoded in source
    must at least be documented — undocumented kinds are how the catalogue
    drifts. (Constants are preferred; `authz.denied` is a pre-sprint
    literal and is documented.)"""
    doc = (REPO / "docs" / "audit" / "event-kinds.md").read_text("utf-8")
    src_dir = REPO / "services" / "core-service" / "src" / "core_service"
    offenders: list[str] = []
    for path in src_dir.rglob("*.py"):
        text = path.read_text("utf-8")
        for match in re.finditer(r'(?<![\w])kind\s*=\s*"(\w+(?:\.\w+)+)"', text):
            if f"`{match.group(1)}`" not in doc:
                offenders.append(f"{path.name}: {match.group(1)}")
    assert not offenders, offenders


# ── Metric labels: tenant/enums only, NEVER ids ─────────────────────


def test_metric_labels_carry_no_identifiers() -> None:
    """Static sweep of every attributes={...} dict in the privacy metric
    emitters: allowed label keys only (cardinality + privacy)."""
    allowed = {"kind", "status"}
    sources = [
        REPO / "scripts" / "jobs" / "erasure_scheduler.py",
        REPO / "services" / "core-service" / "src" / "core_service" / "erasure" / "dsar.py",
        REPO / "services" / "core-service" / "src" / "core_service" / "erasure" / "engine.py",
    ]
    for path in sources:
        text = path.read_text("utf-8")
        for match in re.finditer(r"attributes\s*=\s*\{([^}]*)\}", text):
            keys = set(re.findall(r'"(\w+)"\s*:', match.group(1)))
            assert keys <= allowed, f"{path.name}: disallowed metric labels {keys - allowed}"
