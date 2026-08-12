#!/usr/bin/env python3
"""CI gate — exported metric names must equal the declared instrument names.

The OpenTelemetry Prometheus exporter mangles names by default: it appends
the instrument's *unit* to the exported series. A ``unit="1"`` gauge
becomes ``..._ratio``, a ``unit="ms"`` histogram becomes
``..._milliseconds``, ``unit="MB"`` becomes ``..._MB``. Every alert rule
and Grafana dashboard in this repo queries the name as declared in the
service's ``metrics.py`` ("Names match sprint-04 spec §9 verbatim — the
Grafana dashboard and alerts reference them. Keep stable."), so the
mangling silently pointed all of them at names that do not exist.

Mostly that fails quiet — an alert on a nonexistent series never fires,
which reads as "no problem" on a green dashboard. It also fails LOUD:
``DictationConversationFleetUnavailable`` is written
``(sum(mdx_dictation_conversation_ready) or vector(0)) == 0`` so the
absent name evaluated to 0 and paged "NO worker in the fleet can take a
conversation session" against a healthy, warm, conversation-ready fleet.

Two checks, one per direction of the drift:

1. Every collector config sets ``add_metric_suffixes: false`` on the
   prometheus exporter — the exporter must not mangle names.
2. No rule/dashboard queries a mangled name — i.e. no reference is a
   declared instrument plus a unit suffix. That catches the "fix" of
   chasing the exporter by renaming the rule instead of the config,
   which would break again the moment check 1 is honoured.

This does NOT require every referenced ``mdx_*`` name to be a declared
instrument: several are legitimately produced outside the services, by
Prometheus textfile exporters (``scripts/eval/run_wer.py``,
``infra/k8s/mdx/files/jobs/nightly_verify.py``).

Exit codes:
    0 — no violations
    1 — violations printed to stderr
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

COLLECTOR_CONFIGS = (
    "infra/otel/otel-collector-config.yaml",
    "infra/k8s/mdx/templates/observability.yaml",
)

RULE_DIRS = ("infra/prometheus/rules",)
DASHBOARD_DIRS = ("infra/grafana/dashboards",)

# Unit -> suffix the OTel Prometheus exporter appends. "1" maps to
# "_ratio" for gauges only, but we flag it for any instrument: a rule
# referencing `<declared>_ratio` is wrong either way.
_UNIT_SUFFIX = {
    "1": "ratio",
    "s": "seconds",
    "ms": "milliseconds",
    "us": "microseconds",
    "ns": "nanoseconds",
    "By": "bytes",
    "MB": "MB",
    "bps": "bps",
}

_INSTRUMENT_RE = re.compile(
    r"create_(?:gauge|counter|up_down_counter|histogram"
    r"|observable_gauge|observable_counter|observable_up_down_counter)\("
    r"\s*\"(?P<name>[A-Za-z_][A-Za-z0-9_.]*)\"(?P<rest>.*?)\)\n",
    re.S,
)
_UNIT_RE = re.compile(r"unit\s*=\s*\"(?P<unit>[^\"]*)\"")
_METRIC_REF_RE = re.compile(r"\bmdx_[a-z0-9_]+")
# Histogram/summary series suffixes Prometheus itself appends — strip
# before comparing against an instrument name.
_SERIES_SUFFIX_RE = re.compile(r"_(bucket|count|sum)$")


def declared_instruments() -> dict[str, str]:
    """Map instrument name -> unit, over every service and lib."""
    found: dict[str, str] = {}
    for path in ROOT.rglob("*.py"):
        if any(part in {".venv", "__pycache__", "node_modules"} for part in path.parts):
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "create_" not in source:
            continue
        for match in _INSTRUMENT_RE.finditer(source):
            unit = _UNIT_RE.search(match.group("rest"))
            found[match.group("name")] = unit.group("unit") if unit else ""
    return found


def query_files() -> list[Path]:
    files: list[Path] = []
    for rel in RULE_DIRS:
        files.extend(sorted((ROOT / rel).glob("*.yml")))
        files.extend(sorted((ROOT / rel / "tests").glob("*.yml")))
    for rel in DASHBOARD_DIRS:
        files.extend(sorted((ROOT / rel).glob("*.json")))
    return [f for f in files if f.is_file()]


def check_collector_configs(errors: list[str]) -> None:
    for rel in COLLECTOR_CONFIGS:
        path = ROOT / rel
        if not path.is_file():
            errors.append(f"{rel}: collector config missing")
            continue
        text = path.read_text(encoding="utf-8")
        if not re.search(r"^\s*prometheus:\s*$", text, re.M):
            continue  # no prometheus exporter in this config
        if not re.search(r"^\s*add_metric_suffixes:\s*false\s*$", text, re.M):
            errors.append(
                f"{rel}: prometheus exporter does not set "
                "`add_metric_suffixes: false` — exported names will carry a "
                "unit suffix (_ratio/_milliseconds/...) and every alert rule "
                "will query a series that does not exist"
            )


def check_query_references(instruments: dict[str, str], errors: list[str]) -> None:
    # Longest-first so `mdx_a_b` wins over `mdx_a` when both are declared.
    by_len = sorted(instruments.items(), key=lambda kv: -len(kv[0]))
    for path in query_files():
        rel = path.relative_to(ROOT)
        text = path.read_text(encoding="utf-8")
        for ref in sorted(set(_METRIC_REF_RE.findall(text))):
            stem = _SERIES_SUFFIX_RE.sub("", ref)
            if stem in instruments:
                continue
            for name, unit in by_len:
                suffix = _UNIT_SUFFIX.get(unit)
                if suffix and stem == f"{name}_{suffix}":
                    errors.append(
                        f"{rel}: queries `{ref}` — that is the instrument "
                        f"`{name}` (unit={unit!r}) with the exporter's unit "
                        f"suffix appended. Query `{name}` and keep "
                        "`add_metric_suffixes: false` in the collector config."
                    )
                    break


def main() -> int:
    errors: list[str] = []
    instruments = declared_instruments()
    if not instruments:
        print("check-metric-names: found no instruments to check", file=sys.stderr)
        return 1
    check_collector_configs(errors)
    check_query_references(instruments, errors)

    if errors:
        print("Metric-name drift (exported name != declared instrument):\n", file=sys.stderr)
        for err in errors:
            print(f"  {err}", file=sys.stderr)
        print(
            f"\n{len(errors)} violation(s). See "
            "infra/otel/otel-collector-config.yaml for the why.",
            file=sys.stderr,
        )
        return 1

    print(f"check-metric-names: OK ({len(instruments)} instruments checked)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
