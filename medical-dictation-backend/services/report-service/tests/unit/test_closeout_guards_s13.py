"""Sprint-13 close-out guards: kind registration + metric-label discipline.

Two failure modes these catch:
- a kind emitted in code but absent from the catalogue (the catalogue
  stops being the source of truth the moment that happens);
- an option VALUE reaching a metric label. Option values are
  template-authored, so a label carrying them is unbounded cardinality
  AND clinical vocabulary in the metrics store. field_type only.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
_EVENT_KINDS = REPO / "docs" / "audit" / "event-kinds.md"
_REPORT_SRC = REPO / "services" / "report-service" / "src" / "report_service"
_NLP_SRC = REPO / "services" / "nlp-service" / "src" / "nlp_service"


# ── kind registration ───────────────────────────────────────────────


def test_every_report_service_audit_kind_is_documented() -> None:
    import report_service.audit_kinds as kinds_mod

    doc = _EVENT_KINDS.read_text("utf-8")
    missing = [
        v
        for k, v in vars(kinds_mod).items()
        if not k.startswith("_") and isinstance(v, str) and "." in v and f"`{v}`" not in doc
    ]
    assert not missing, f"emitted kinds missing from event-kinds.md: {missing}"


def test_every_nlp_service_audit_kind_is_documented() -> None:
    import nlp_service.audit_kinds as kinds_mod

    doc = _EVENT_KINDS.read_text("utf-8")
    missing = [
        v
        for k, v in vars(kinds_mod).items()
        if not k.startswith("_") and isinstance(v, str) and "." in v and f"`{v}`" not in doc
    ]
    assert not missing, f"emitted kinds missing from event-kinds.md: {missing}"


def test_the_three_sprint_13_kinds_are_registered() -> None:
    doc = _EVENT_KINDS.read_text("utf-8")
    for kind in (
        "anamnesis.field.extracted",
        "anamnesis.field.confirmed",
        "anamnesis.field.overridden",
    ):
        assert f"`{kind}`" in doc, kind


def test_icd10_searched_deviation_is_recorded() -> None:
    """It is metrics-only ON PURPOSE — the reason must be written down,
    or a future reader will 'fix' the missing kind."""
    doc = _EVENT_KINDS.read_text("utf-8")
    assert "icd10.searched" in doc
    assert "metrics-only" in doc.lower()


def test_icd10_search_emits_no_audit_event() -> None:
    """Guard the deviation in code, not just in prose."""
    source = (_REPORT_SRC / "routers" / "icd10.py").read_text("utf-8")
    assert "audit_writer" not in source
    assert "write_event" not in source


# ── metric-label discipline ─────────────────────────────────────────


def _counter_label_keys(path: Path, counter_names: set[str]) -> set[str]:
    """Static label keys passed to `.add(...)` on the named counters."""
    tree = ast.parse(path.read_text("utf-8"))
    # counter variable name → metric name
    var_to_metric: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            func = node.value.func
            if getattr(func, "attr", None) == "create_counter" and node.value.args:
                first = node.value.args[0]
                if isinstance(first, ast.Constant) and first.value in counter_names:
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            var_to_metric[target.id] = first.value

    keys: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "add":
            for arg in node.args[1:]:
                if isinstance(arg, ast.Dict):
                    keys.update(k.value for k in arg.keys if isinstance(k, ast.Constant))
    return keys


def test_quality_counters_label_only_by_field_type() -> None:
    keys = _counter_label_keys(
        _REPORT_SRC / "routers" / "reports_drafts.py",
        {"mdx_field_confirmed_total", "mdx_field_overridden_total"},
    )
    assert keys <= {"field_type"}, f"unexpected metric labels: {keys}"


def test_extraction_counter_labels_are_bounded_enums() -> None:
    """nlp-service's outcome counter: field_type / outcome / language —
    all closed sets. Never a section key or an option value."""
    keys = _counter_label_keys(
        _NLP_SRC / "stages" / "field_extraction.py", {"mdx_field_extraction_total"}
    )
    assert keys <= {"field_type", "outcome", "language"}, f"unexpected labels: {keys}"


def test_no_option_value_ever_reaches_a_metric_label() -> None:
    """The rule that keeps the pattern safe for future field types."""
    for path in [
        _REPORT_SRC / "routers" / "reports_drafts.py",
        _NLP_SRC / "stages" / "field_extraction.py",
    ]:
        source = path.read_text("utf-8")
        for forbidden in ('"value"', '"selected"', '"section_key"', '"code"'):
            # These may appear in audit payloads (bounded, offline-analysed)
            # but never inside a metric .add(...) attribute dict.
            for call in re.findall(r"\.add\((.*?)\)\n", source, re.DOTALL):
                assert forbidden not in call, f"{path.name}: {forbidden} in a metric label"


# ── alert rules ─────────────────────────────────────────────────────

_RULES = REPO / "infra" / "prometheus" / "rules" / "sprint-13-alerts.yml"


def test_sprint_13_alert_rules_exist() -> None:
    assert _RULES.exists()


def test_alerts_reference_metrics_that_are_actually_emitted() -> None:
    """An alert on a metric nobody emits is a silent no-op."""
    rules = _RULES.read_text("utf-8")
    emitted = "\n".join(
        p.read_text("utf-8")
        for p in [
            _REPORT_SRC / "routers" / "reports_drafts.py",
            _REPORT_SRC / "routers" / "icd10.py",
            _NLP_SRC / "stages" / "field_extraction.py",
            _NLP_SRC / "stages" / "voice_commands.py",
        ]
    )
    for metric in re.findall(r"\b(mdx_[a-z0-9_]+)_(?:total|bucket|seconds|count|sum)\b", rules):
        base = metric
        assert base in emitted, f"alert references unemitted metric: {base}"


def test_every_alert_has_a_runbook_anchor() -> None:
    rules = _RULES.read_text("utf-8")
    alerts = re.findall(r"- alert: (\w+)", rules)
    runbooks = re.findall(r"runbook: ", rules)
    assert len(alerts) == len(runbooks) == 3, (alerts, len(runbooks))


_DASHBOARD = REPO / "infra" / "grafana" / "dashboards" / "sprint-13-extraction.json"


def test_dashboard_exists_and_has_the_override_rate_panel() -> None:
    import json

    dash = json.loads(_DASHBOARD.read_text("utf-8"))
    assert dash["uid"] == "sprint-13-extraction"
    titles = [p.get("title", "") for p in dash["panels"]]
    assert any("Override rate" in t for t in titles), titles


def test_dashboard_references_only_emitted_metrics() -> None:
    """A panel querying a metric nobody emits renders an empty graph
    forever — worse than no panel, because it looks like zero traffic."""
    import json

    dash = json.loads(_DASHBOARD.read_text("utf-8"))
    emitted = "\n".join(
        p.read_text("utf-8")
        for p in [
            _REPORT_SRC / "routers" / "reports_drafts.py",
            _REPORT_SRC / "routers" / "icd10.py",
            _NLP_SRC / "stages" / "field_extraction.py",
            _NLP_SRC / "stages" / "voice_commands.py",
        ]
    )
    exprs = " ".join(t.get("expr", "") for p in dash["panels"] for t in p.get("targets", []))
    for metric in set(
        re.findall(r"\b(mdx_[a-z0-9_]+?)(?:_bucket|_total|_seconds|_count|_sum)?\b", exprs)
    ):
        base = re.sub(r"_(bucket|count|sum)$", "", metric)
        assert base in emitted, f"dashboard references unemitted metric: {base}"


def test_dashboard_never_labels_by_option_value() -> None:
    """Cardinality + clinical-vocabulary discipline, enforced on the
    dashboard too (a `by (value)` would only fail at query time)."""
    import json

    dash = json.loads(_DASHBOARD.read_text("utf-8"))
    exprs = " ".join(t.get("expr", "") for p in dash["panels"] for t in p.get("targets", []))
    for forbidden in ("by (value)", "by (selected)", "by (code)", "by (section_key)"):
        assert forbidden not in exprs, forbidden
