#!/usr/bin/env python3
"""CI gate — render the Helm chart and police the RENDERED output.

Three checks, run for both staging and prod values:

1. `helm template` must succeed (chart always renders).
2. The PROD render must be secret-clean: no `dev-secret-change-in-prod`,
   no `dev-password`, and none of the demo/dev escape hatches
   (`MD_OBJECT_STORE_DISABLED`, `MDX_DEMO_MODE`, `AUTH_BYPASS_DEV`,
   `SIGNING_DEV_PASSWORD_ENABLED`) set truthy — the sprint-09/16 config
   gates applied to what the cluster would actually receive.
3. The chart's vendored ops artefacts must not drift from their source
   of truth (files/jobs/*.py ← scripts/jobs/, files/postgres-init.sql ←
   infra/postgres/init.sql).

Requires `helm` on PATH (the k8s CI job installs it; skip locally with
SKIP_HELM=1, which still runs the drift check).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "infra" / "k8s" / "mdx"

FORBIDDEN_LITERALS = ["dev-secret-change-in-prod", "dev-password"]
TRUTHY_FLAGS = re.compile(
    r"(MD_OBJECT_STORE_DISABLED|MDX_DEMO_MODE|AUTH_BYPASS_DEV|"
    r"SIGNING_DEV_PASSWORD_ENABLED)\W+['\"]?(true|1|yes|on)['\"]?",
    re.IGNORECASE,
)

DRIFT_PAIRS = [
    ("files/jobs/erasure_scheduler.py", "scripts/jobs/erasure_scheduler.py"),
    ("files/jobs/dsar_package_cleanup.py", "scripts/jobs/dsar_package_cleanup.py"),
    ("files/jobs/nightly_verify.py", "scripts/jobs/nightly_verify.py"),
    ("files/postgres-init.sql", "infra/postgres/init.sql"),
]


def render(values: Path | None) -> str:
    cmd = ["helm", "template", "mdx", str(CHART)]
    if values is not None:
        cmd += ["-f", str(values)]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        print(f"helm template failed ({values or 'staging defaults'}):\n{out.stderr}",
              file=sys.stderr)
        sys.exit(1)
    return out.stdout


def main() -> int:
    failures: list[str] = []

    for chart_rel, src_rel in DRIFT_PAIRS:
        chart_file = CHART / chart_rel
        src_file = ROOT / src_rel
        if chart_file.read_bytes() != src_file.read_bytes():
            failures.append(
                f"chart file {chart_rel} drifted from {src_rel} — "
                f"re-copy it (cp {src_rel} infra/k8s/mdx/{chart_rel})"
            )

    if shutil.which("helm") and not os.environ.get("SKIP_HELM"):
        render(None)  # staging must render
        prod = render(CHART / "values-prod.yaml")
        for literal in FORBIDDEN_LITERALS:
            if literal in prod:
                failures.append(f"prod render contains forbidden literal {literal!r}")
        m = TRUTHY_FLAGS.search(prod)
        if m:
            failures.append(f"prod render enables demo/dev escape hatch {m.group(1)}")
        print("check-k8s-rendered: staging + prod rendered; prod is secret-clean")
    else:
        print("check-k8s-rendered: helm not available — drift check only")

    if failures:
        print("check-k8s-rendered: FAILURES:", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        return 1
    print("check-k8s-rendered: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
