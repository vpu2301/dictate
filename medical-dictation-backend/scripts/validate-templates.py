#!/usr/bin/env python3
"""CI gate — validate every template JSON file in ``infra/seeds/templates/``.

Each file must:
- Parse against ``TemplateDefinition`` Pydantic model (which enforces
  the sprint-13 options rules: choice/multi_choice sections carry 2..50
  options, other field types carry none; values/labels/aliases unique).
- Have ``asr_prompt`` ≤ 224 tokens per section (tiktoken cl100k_base).
- Have unique ``voice_aliases`` across sections (the model enforces this).
- File name must match ``code.json``.
- Contain no PII in option labels/aliases (sprint-13): template options
  are shared clinical vocabulary, same rule as the autocomplete corpus.

Run::

    python scripts/validate-templates.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

try:
    import tiktoken

    _enc = tiktoken.get_encoding("cl100k_base")

    def count_tokens(text: str) -> int:
        return len(_enc.encode(text))

except ImportError:
    # Fallback: 4-char-per-token approximation. CI installs tiktoken.
    def count_tokens(text: str) -> int:
        return max(1, len(text) // 4)


SEED_DIR = Path(__file__).resolve().parents[1] / "infra" / "seeds" / "templates"
ASR_PROMPT_MAX_TOKENS = 224

# Mirror of autocomplete_service.scrubber._PATTERNS (same set as
# scripts/validate-autocomplete-corpus.py) — template options must never
# contain patient data.
PII_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")),
    ("ipn", re.compile(r"\b\d{10}\b")),
    ("med_id", re.compile(r"\b\d{13}\b")),
    ("passport", re.compile(r"\b[A-Za-zА-ЯЇІЄҐа-яїієґ]{2}\s?\d{6}\b")),
    ("dob_like", re.compile(r"\b\d{1,2}[./-]\d{1,2}[./-]\d{4}\b")),
    ("phone", re.compile(r"(?<![\d+])\+?\d{7,14}(?!\d)")),
]


def _pii_hits(text: str) -> list[str]:
    return [name for name, pat in PII_PATTERNS if pat.search(text)]


def main() -> int:
    sys.path.insert(
        0, str(Path(__file__).resolve().parents[1] / "libs" / "template_models" / "src")
    )
    from template_models import TemplateDefinition

    failures: list[str] = []
    seen_codes: set[tuple[str, str]] = set()

    if not SEED_DIR.exists():
        print(f"warn: seed dir {SEED_DIR} does not exist (skipping)")
        return 0

    files = sorted(SEED_DIR.glob("*.json"))
    if not files:
        print(f"warn: no template files in {SEED_DIR}")
        return 0

    for path in files:
        try:
            doc = json.loads(path.read_text("utf-8"))
        except json.JSONDecodeError as exc:
            failures.append(f"{path.name}: invalid JSON — {exc}")
            continue

        try:
            tpl = TemplateDefinition.model_validate(doc)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{path.name}: schema validation failed — {exc}")
            continue

        # File name must match ``code.json``.
        expected_filename = f"{tpl.code}.json"
        if path.name != expected_filename:
            failures.append(
                f"{path.name}: file name does not match code ({expected_filename} expected)"
            )

        # Duplicate code+language pair.
        key = (tpl.code, tpl.language)
        if key in seen_codes:
            failures.append(f"{path.name}: duplicate (code,language)={key}")
        seen_codes.add(key)

        # Per-section ASR prompt token budget.
        for section in tpl.sections:
            n = count_tokens(section.asr_prompt)
            if n > ASR_PROMPT_MAX_TOKENS:
                failures.append(
                    f"{path.name} section={section.id}: asr_prompt is "
                    f"{n} tokens (max {ASR_PROMPT_MAX_TOKENS})"
                )

        # Sprint-13: PII sweep over choice options.
        n_choice = 0
        for section in tpl.sections:
            if not section.options:
                continue
            n_choice += 1
            for opt in section.options:
                for text, where in [
                    (opt.label, f"option={opt.value} label"),
                    *((a, f"option={opt.value} alias {a!r}") for a in opt.voice_aliases),
                ]:
                    hits = _pii_hits(text)
                    if hits:
                        failures.append(
                            f"{path.name} section={section.id} {where}: "
                            f"PII pattern(s) matched: {', '.join(hits)}"
                        )

        suffix = f", {n_choice} choice" if n_choice else ""
        print(
            f"ok: {path.name} — {tpl.specialty}/{tpl.language} "
            f"({len(tpl.sections)} sections{suffix})"
        )

    if failures:
        print("\n=== FAILURES ===", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        return 1
    print(f"\n{len(files)} template files validated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
