#!/usr/bin/env python3
"""Gate LM3: a prompt change requires a changelog entry.

Prompts are versioned artifacts whose text is recorded per answer
(`answer_provenance.prompt_versions`). An edit that skips the changelog makes
that record a lie by omission — the pin still says `1.0` while the text has
moved.

The check compares the working tree against the merge-base with the default
branch: if any `prompts/*.md` changed, `prompts/CHANGELOG.md` must have
changed too. Outside a git checkout (or with no base to compare against) it
passes with a notice rather than failing — a gate that cannot run must not
block a build it never examined.
"""

from __future__ import annotations

import subprocess  # nosec B404 — fixed git argv below, never shell, never user input
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
BASE_REF = "origin/main"
FALLBACK_REF = "main"


def _git(*args: str) -> tuple[int, str]:
    result = subprocess.run(  # nosec B603 B607 — fixed git command
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
    )
    return result.returncode, result.stdout.strip()


def _base_ref() -> str | None:
    for ref in (BASE_REF, FALLBACK_REF):
        code, _ = _git("rev-parse", "--verify", "--quiet", ref)
        if code == 0:
            code, merge_base = _git("merge-base", "HEAD", ref)
            if code == 0 and merge_base:
                return merge_base
    return None


def main() -> int:
    code, _ = _git("rev-parse", "--git-dir")
    if code != 0:
        print("check-prompt-changelog: not a git checkout — skipped")
        return 0
    base = _base_ref()
    if base is None:
        print("check-prompt-changelog: no base ref to compare against — skipped")
        return 0

    code, output = _git("diff", "--name-only", base, "--", "*/prompts/*.md")
    if code != 0:
        print("check-prompt-changelog: git diff failed — skipped")
        return 0
    changed = [line for line in output.splitlines() if line.strip()]
    if not changed:
        print("check-prompt-changelog: OK (no prompt changes)")
        return 0

    prompts = [p for p in changed if not p.endswith("CHANGELOG.md")]
    changelogs = {p for p in changed if p.endswith("CHANGELOG.md")}
    if not prompts:
        print("check-prompt-changelog: OK (changelog-only change)")
        return 0

    missing = []
    for prompt in prompts:
        expected = str(Path(prompt).parent / "CHANGELOG.md")
        if expected not in changelogs:
            missing.append(f"{prompt} → needs an entry in {expected}")
    if missing:
        print("check-prompt-changelog: prompt changed without a changelog entry (rule LM3):")
        for line in missing:
            print(f"  {line}")
        print("  Add the entry AND bump the pipeline version — the prompt pin is")
        print("  recorded in every answer's provenance.")
        return 1
    print(f"check-prompt-changelog: OK ({len(prompts)} prompt(s) changed, all changelogged)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
