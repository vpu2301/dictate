"""Sprint-12 alert rules stay loadable and on-contract.

promtool is not in the venv, so this validates what CI can: the YAML
parses, the contract rule names exist with fixed severities, and — the
important one — every metric an alert references is a metric the service
actually creates.

That last check is the sprint-10 post-mortem encoded as a test. There,
alerts named metrics nothing exported, so they could never fire and the
feature was silently broken for weeks. An alert that cannot fire is
worse than no alert: it reads as coverage.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[4]
RULES = REPO / "infra" / "prometheus" / "rules" / "sprint-12-alerts.yml"
METRICS_SRC = (
    REPO
    / "services"
    / "notification-service"
    / "src"
    / "notification_service"
    / "metrics.py"
)

EXPECTED: dict[str, str] = {
    "NotificationStreamLagHigh": "page",
    "NotificationConsumerStalled": "page",
    "NotificationDeliveryFailureRateHigh": "page",
    "NotificationDeadLetterPresent": "page",
    "NotificationDigestStale": "ticket",
    "NotificationCoalescingSustained": "ticket",
    "NotificationFanoutLatencyHigh": "ticket",
}


def _rules() -> list[dict]:
    doc = yaml.safe_load(RULES.read_text())
    return doc["groups"][0]["rules"]


def test_rules_file_parses() -> None:
    doc = yaml.safe_load(RULES.read_text())
    assert doc["groups"][0]["name"] == "sprint-12-notifications"


def test_expected_alerts_exist_with_correct_severity() -> None:
    by_name = {r["alert"]: r for r in _rules()}
    assert set(by_name) == set(EXPECTED)
    for name, severity in EXPECTED.items():
        assert by_name[name]["labels"]["severity"] == severity


def test_every_alert_has_a_runbook_link() -> None:
    """An alert without a runbook wakes someone with no next step."""
    for rule in _rules():
        assert rule["annotations"]["runbook"].startswith("docs/runbooks/notifications.md#")
        assert rule["annotations"]["summary"]


def test_alert_metrics_are_actually_emitted() -> None:
    """No alert may reference a metric the service never creates."""
    declared = set(re.findall(r'"(mdx_notification_[a-z_]+)"', METRICS_SRC.read_text()))
    assert declared, "no metrics found — the regex or the module moved"

    referenced: set[str] = set()
    for rule in _rules():
        referenced |= set(re.findall(r"mdx_notification_[a-z_]+", rule["expr"]))

    # Histograms are queried as `<name>_bucket`; counters sometimes as
    # `<name>` directly. Normalise the Prometheus suffix before comparing.
    normalised = {m[: -len("_bucket")] if m.endswith("_bucket") else m for m in referenced}

    missing = normalised - declared
    assert not missing, (
        f"alerts reference metrics that notification-service never emits: "
        f"{sorted(missing)} — these alerts can never fire"
    )


def test_failure_ratio_alert_does_not_clamp_the_denominator() -> None:
    """clamp_min(…, 1) turns a ratio into absolute rate and false-fires.

    Same defect as sprint-10's JwksCacheHitRatioLow: below one event per
    second the clamped denominator makes the expression read as
    failures/sec, which trips overnight when traffic is near zero.
    """
    expr = next(
        r["expr"] for r in _rules() if r["alert"] == "NotificationDeliveryFailureRateHigh"
    )
    assert "clamp_min" not in expr
    assert "or vector(0)" in expr
