"""Step-07 §8 — sprint-10 alert rules stay loadable and on-contract.

promtool is not in the venv; this validates what CI can: the YAML
parses, the five contract rule names exist with the fixed severities,
and every referenced metric is one the service actually emits (the
sprint-10 root-cause was exactly this drift: dashboards/alerts naming
metrics nothing exported).
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[4]
RULES = REPO / "infra" / "prometheus" / "rules" / "sprint-10-alerts.yml"
SRC = REPO / "services" / "autocomplete-service" / "src"

EXPECTED = {
    "AutocompleteSuggestLatencyHigh": "page",
    "AutocompleteCacheHitRatioLow": "warn",
    "AutocompleteScrubberRedactionSpike": "warn",
    "AutocompletePhraseWritePiiRejectionSpike": "warn",
    "AutocompleteRollupMissed": "page",
}


def _rules() -> list[dict]:
    doc = yaml.safe_load(RULES.read_text("utf-8"))
    (group,) = doc["groups"]
    return group["rules"]


def test_five_rules_with_fixed_names_and_severities():
    rules = {r["alert"]: r for r in _rules()}
    assert set(rules) == set(EXPECTED)
    for name, severity in EXPECTED.items():
        assert rules[name]["labels"]["severity"] == severity, name
        assert "runbook" in rules[name]["annotations"], f"{name} missing runbook anchor"


def test_rules_are_loaded_from_the_directory_prometheus_actually_reads():
    # The compose Prometheus loads /etc/prometheus/rules/*.yml which mounts
    # infra/prometheus/rules — a rules file anywhere else never loads.
    assert RULES.exists()


def test_every_metric_referenced_is_actually_emitted():
    emitted = set()
    for p in SRC.rglob("*.py"):
        emitted.update(re.findall(r'"(mdx_autocomplete_[a-z0-9_]+)"', p.read_text("utf-8")))
    # histogram instruments export _bucket/_count/_sum
    for h in [m for m in emitted if "histogram" in m or m.endswith("_seconds") or m.endswith("_bytes")]:
        emitted.update({f"{h}_bucket", f"{h}_count", f"{h}_sum"})

    text = RULES.read_text("utf-8")
    referenced = set(re.findall(r"(mdx_autocomplete_[a-z0-9_]+)", text))
    unknown = referenced - emitted
    assert not unknown, f"alert rules reference metrics nothing emits: {sorted(unknown)}"
