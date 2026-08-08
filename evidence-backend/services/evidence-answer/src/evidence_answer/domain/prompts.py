"""Prompt loader (rule LM3: prompts are versioned files with changelogs).

Files carry YAML-ish front matter (`name`, `version`, `role`,
`temperature_max`) which is parsed without a YAML dependency — the schema is
four scalar keys and adding a parser for it would be more surface than value.

Two things the loader gives the pipeline:

* `version` strings for `answer_provenance.prompt_versions`, so any answer can
  be traced to the exact prompt text;
* a `sha256` fingerprint of the file, which is what actually detects an edit —
  a version bump the author forgot to make would otherwise be invisible.

The HTML comment blocks in the prompt files are rationale for the humans
editing them and are stripped before the text reaches a model.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"

_FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_COMMENT = re.compile(r"<!--.*?-->\s*", re.DOTALL)


@dataclass(frozen=True, slots=True)
class Prompt:
    name: str
    version: str
    role: str
    temperature_max: float
    text: str
    sha256: str

    @property
    def pin(self) -> str:
        """What goes into provenance: version plus a fingerprint prefix."""
        return f"{self.version}+{self.sha256[:12]}"


def _parse_front_matter(raw: str) -> tuple[dict[str, str], str]:
    match = _FRONT_MATTER.match(raw)
    if match is None:
        raise ValueError("prompt file is missing front matter")
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip().strip('"').strip("'")
    return fields, raw[match.end() :]


@cache
def load(filename: str) -> Prompt:
    path = PROMPT_DIR / filename
    raw = path.read_text(encoding="utf-8")
    fields, body = _parse_front_matter(raw)
    missing = {"name", "version", "role"} - fields.keys()
    if missing:
        raise ValueError(f"{filename}: front matter missing {sorted(missing)}")
    return Prompt(
        name=fields["name"],
        version=fields["version"],
        role=fields["role"],
        temperature_max=float(fields.get("temperature_max", "0.2")),
        text=_COMMENT.sub("", body).strip(),
        sha256=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
    )


def triage_prompt() -> Prompt:
    return load("triage_v1.md")


def intent_prompt() -> Prompt:
    return load("intent_v1.md")


def synthesis_prompt() -> Prompt:
    return load("synthesis_v1.md")


def all_versions() -> dict[str, str]:
    """`{prompt name: pin}` for `answer_provenance.prompt_versions`."""
    return {p.name: p.pin for p in (triage_prompt(), intent_prompt(), synthesis_prompt())}
