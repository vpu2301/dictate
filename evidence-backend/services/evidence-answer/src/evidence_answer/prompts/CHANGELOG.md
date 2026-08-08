# Prompt changelog

Rule LM3: prompts are versioned files with changelogs; a prompt change is a
pipeline-version bump and triggers the standing gate. CI enforces the second
half of that mechanically — `scripts/dev/check_prompt_changelog.py` fails when
a `prompts/*.md` file changes without a new entry here (see `make
check-prompt-changelog`).

Prompt versions are recorded per answer in
`answer_provenance.prompt_versions`, so any answer can be traced back to the
exact text that produced it.

## 1.0 — 2026-08-06 (EVA-S04)

| Prompt | Role | Notes |
|---|---|---|
| `triage_v1.md` | `generator.fast` | Second-stage classifier behind the deterministic rule list. Fail-open on classifier outage (the rules already ran); `emergency`/`self_harm` are the only bias-to-deflect codes |
| `intent_v1.md` | `generator.fast` | JSON-constrained `ClinicalIntent` extraction. Carries the QS1 instruction: concepts are clinical terms, never identifiers or verbatim sentences |
| `synthesis_v1.md` | `generator.heavy` | Three-part structure (instruction hierarchy → delimited evidence → line grammar). `[PLACEMENT\|kind] text [Sn]` lines, one segment per line, so the parser can stream. `evidence` without a citation is invalid output (ET2 enforced again downstream by the binder and the envelope validator) |

Pipeline version at release: `quick-search-1.0`.
