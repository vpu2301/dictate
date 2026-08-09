"""S11 step 08 — privacy alert rules stay loadable and on-contract.

Mirrors the S10 method: the YAML parses, the contract rule names exist
with fixed severities, and every referenced metric is one the emitters
actually produce (the S10 root-cause was exactly this drift).

NOTE (hotfix): `DsarExportFailed` was added to the rules file in S16-17
but never added to ``EXPECTED``, so this test had been failing since
then — unnoticed, because core-service was missing from the ``make test``
target. Both are corrected: the rule is listed below, and core-service
now runs in CI.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[4]
RULES = REPO / "infra" / "prometheus" / "rules" / "sprint-11-privacy.yml"
EMITTERS = [
    REPO / "scripts" / "jobs" / "erasure_scheduler.py",
    REPO / "services" / "core-service" / "src" / "core_service" / "erasure" / "dsar.py",
    REPO / "services" / "core-service" / "src" / "core_service" / "erasure" / "engine.py",
]

EXPECTED = {
    "ErasureRequestStuckExecuting": "page",
    "ErasureApprovedOverdue": "page",
    "DsarExportSlow": "warn",
    "DsarExportFailed": "warn",
    "ErasureExecutionError": "warn",
}


def _rules() -> list[dict]:
    doc = yaml.safe_load(RULES.read_text("utf-8"))
    (group,) = doc["groups"]
    return group["rules"]


def test_contract_rules_have_fixed_names_and_severities() -> None:
    rules = {r["alert"]: r for r in _rules()}
    assert set(rules) == set(EXPECTED)
    for name, severity in EXPECTED.items():
        assert rules[name]["labels"]["severity"] == severity, name


def test_every_referenced_metric_is_emitted() -> None:
    emitted = "\n".join(p.read_text("utf-8") for p in EMITTERS)
    for rule in _rules():
        for metric in set(re.findall(r"(mdx_[a-z0-9_]+)", rule["expr"])):
            assert f'"{metric}"' in emitted, f"{rule['alert']} references {metric} — not emitted"
