#!/usr/bin/env python3
"""Additive-only contract linter (rule RC1: additive changes only; breaking => new major + ADR).

Compares the working-tree schemas in docs/api/evidence-contracts/ against the
last released tag (``evidence-contracts-v*``). A breaking change without a
major-version bump exits 1. First release (no tag yet) passes with a notice.

Breaking, per contract:
  - contract file removed
  - property removed (top level or inside any $defs entry)
  - property type changed
  - enum value removed
  - previously-optional property made required (old producers stop validating)
  - $defs entry removed
  - x-contract-version lowered

Additive (allowed without a major bump): new optional properties, new enum
values, new $defs, new contracts, minor-version bumps.
"""

from __future__ import annotations

import json
import subprocess  # nosec B404 — fixed git argv below, never shell, never user input
from pathlib import Path
from typing import Any

REPO_ROOT_HINT = Path(__file__).resolve().parent.parent.parent
CONTRACTS_DIR = "evidence-backend/docs/api/evidence-contracts"
TAG_PREFIX = "evidence-contracts-v"

Schema = dict[str, Any]


def _git(*args: str) -> str:
    return subprocess.run(  # nosec B603 B607 — argv is a fixed git command
        ["git", *args], cwd=REPO_ROOT_HINT, capture_output=True, text=True, check=True
    ).stdout


def latest_release_tag() -> str | None:
    out = _git("tag", "-l", f"{TAG_PREFIX}*", "--sort=-v:refname")
    tags = [t for t in out.splitlines() if t.strip()]
    return tags[0] if tags else None


def schemas_at_tag(tag: str) -> dict[str, Schema]:
    files = _git("ls-tree", "-r", "--name-only", tag, CONTRACTS_DIR).splitlines()
    result: dict[str, Schema] = {}
    for f in files:
        if f.endswith(".schema.json"):
            result[Path(f).name] = json.loads(_git("show", f"{tag}:{f}"))
    return result


def schemas_in_worktree() -> dict[str, Schema]:
    directory = REPO_ROOT_HINT / "docs" / "api" / "evidence-contracts"
    return {p.name: json.loads(p.read_text()) for p in sorted(directory.glob("*.schema.json"))}


def _object_breaks(old: Schema, new: Schema, where: str) -> list[str]:
    """Breaking differences between two object schemas (one nesting level of keywords)."""
    breaks: list[str] = []
    old_props: dict[str, Any] = old.get("properties", {})
    new_props: dict[str, Any] = new.get("properties", {})
    for prop, old_def in old_props.items():
        if prop not in new_props:
            breaks.append(f"{where}: property {prop!r} removed")
            continue
        new_def = new_props[prop]
        for type_key in ("type", "$ref", "const"):
            if old_def.get(type_key) != new_def.get(type_key):
                breaks.append(
                    f"{where}.{prop}: {type_key} changed "
                    f"{old_def.get(type_key)!r} -> {new_def.get(type_key)!r}"
                )
        old_enum = old_def.get("enum")
        new_enum = new_def.get("enum")
        if old_enum and new_enum:
            removed = set(map(str, old_enum)) - set(map(str, new_enum))
            if removed:
                breaks.append(f"{where}.{prop}: enum values removed {sorted(removed)}")
    old_required = set(old.get("required", []))
    new_required = set(new.get("required", []))
    for newly_required in new_required - old_required:
        if newly_required in old_props:
            breaks.append(f"{where}: existing property {newly_required!r} became required")
    old_enum = old.get("enum")
    new_enum = new.get("enum")
    if old_enum and new_enum:
        removed = set(map(str, old_enum)) - set(map(str, new_enum))
        if removed:
            breaks.append(f"{where}: enum values removed {sorted(removed)}")
    return breaks


def compare_schemas(old: Schema, new: Schema, name: str) -> list[str]:
    breaks = _object_breaks(old, new, name)
    old_defs: dict[str, Schema] = old.get("$defs", {})
    new_defs: dict[str, Schema] = new.get("$defs", {})
    for def_name, old_def in old_defs.items():
        if def_name not in new_defs:
            breaks.append(f"{name}: $defs entry {def_name!r} removed")
        else:
            breaks.extend(_object_breaks(old_def, new_defs[def_name], f"{name}.$defs.{def_name}"))
    return breaks


def _version_tuple(schema: Schema) -> tuple[int, int]:
    raw = str(schema.get("x-contract-version", "0.0"))
    major, minor = raw.split(".")[:2]
    return int(major), int(minor)


def lint(old_schemas: dict[str, Schema], new_schemas: dict[str, Schema]) -> list[str]:
    problems: list[str] = []
    for filename, old_schema in old_schemas.items():
        if filename not in new_schemas:
            problems.append(f"{filename}: released contract removed (breaking; needs v2 + ADR)")
            continue
        new_schema = new_schemas[filename]
        old_version = _version_tuple(old_schema)
        new_version = _version_tuple(new_schema)
        if new_version < old_version:
            problems.append(
                f"{filename}: x-contract-version lowered {old_version} -> {new_version}"
            )
        breaks = compare_schemas(old_schema, new_schema, filename)
        if breaks and new_version[0] <= old_version[0]:
            problems.extend(f"{b}  [breaking without a major-version bump]" for b in breaks)
    return problems


def main() -> int:
    tag = latest_release_tag()
    if tag is None:
        print(f"lint_additive: no {TAG_PREFIX}* tag yet — first release, nothing to compare.")
        return 0
    old_schemas = schemas_at_tag(tag)
    new_schemas = schemas_in_worktree()
    problems = lint(old_schemas, new_schemas)
    if problems:
        print(f"lint_additive: BREAKING contract changes vs {tag}:")
        for p in problems:
            print(f"  {p}")
        print(
            "Additive-only policy (rule RC1): breaking changes require a new major "
            "version, a new contract file, and an ADR."
        )
        return 1
    print(f"lint_additive: OK — contracts are additive-compatible with {tag}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
