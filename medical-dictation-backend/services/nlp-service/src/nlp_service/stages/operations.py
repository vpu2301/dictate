"""Operations dispatch — map a CommandSlot's intent → frontend Operation.

The frontend (sprint 04 + 06) consumes Operations to mutate editor
state. This module is the contract between intents (linguistic) and
operations (UI-side). Adding a new intent without an op is a bug; the
test suite enforces a 1:1 mapping.

Sprint 13 adds the anamnesis typed-field ops (``set_choice``,
``add_choice``, ``remove_choice``, ``mark_diagnosis_text``). They are
additive and FE-stable: a client that ignores them behaves exactly as
before. Full arg shapes: ``docs/nlp/voice-commands.md``.
"""

from __future__ import annotations

from ..pipeline.base import CommandSlot, Operation

_TABLE: dict[str, tuple[str, dict[str, str] | None]] = {
    "newparagraph": ("insert_paragraph_break", None),
    "newline": ("insert_line_break", None),
    "period": ("insert_punctuation", {"value": "."}),
    "comma": ("insert_punctuation", {"value": ","}),
    "question_mark": ("insert_punctuation", {"value": "?"}),
    "dash": ("insert_punctuation", {"value": "—"}),
    "colon": ("insert_punctuation", {"value": ":"}),
    "exclamation_mark": ("insert_punctuation", {"value": "!"}),
    "semicolon": ("insert_punctuation", {"value": ";"}),
    "ellipsis": ("insert_punctuation", {"value": "…"}),
    "hyphen": ("insert_punctuation", {"value": "-"}),
    "en_dash": ("insert_punctuation", {"value": "–"}),
    "open_double_quote": ("insert_punctuation", {"value": "“"}),
    "close_double_quote": ("insert_punctuation", {"value": "”"}),
    "open_single_quote": ("insert_punctuation", {"value": "‘"}),
    "close_single_quote": ("insert_punctuation", {"value": "’"}),
    "open_paren": ("insert_punctuation", {"value": "("}),
    "close_paren": ("insert_punctuation", {"value": ")"}),
    "open_bracket": ("insert_punctuation", {"value": "["}),
    "close_bracket": ("insert_punctuation", {"value": "]"}),
    "open_brace": ("insert_punctuation", {"value": "{"}),
    "close_brace": ("insert_punctuation", {"value": "}"}),
    "slash": ("insert_punctuation", {"value": "/"}),
    "backslash": ("insert_punctuation", {"value": "\\"}),
    "apostrophe": ("insert_punctuation", {"value": "’"}),
    "asterisk": ("insert_punctuation", {"value": "*"}),
    "hash": ("insert_punctuation", {"value": "#"}),
    "numero_sign": ("insert_punctuation", {"value": "№"}),
    "percent_sign": ("insert_punctuation", {"value": "%"}),
    "ampersand": ("insert_punctuation", {"value": "&"}),
    "at_sign": ("insert_punctuation", {"value": "@"}),
    "plus_sign": ("insert_punctuation", {"value": "+"}),
    "minus_sign": ("insert_punctuation", {"value": "−"}),
    "equals_sign": ("insert_punctuation", {"value": "="}),
    "underscore": ("insert_punctuation", {"value": "_"}),
    "vertical_bar": ("insert_punctuation", {"value": "|"}),
    "save_draft": ("save_draft", None),
    "undo_last": ("undo_last", None),
    "stop_dictation": ("stop_dictation", None),
    "begin_quote": ("insert_quote_marker", {"value": "open"}),
    "end_quote": ("insert_quote_marker", {"value": "close"}),
    "insert_template": ("insert_template", None),
    # ── Sprint 13: anamnesis typed-field commands ──────────────────
    # arg: {section_key, value} — ``value`` is always the option SLUG,
    # never the spoken words. A voice selection is an explicit clinician
    # act, so the FE writes it as ``source: "manual"`` metadata (never
    # "extracted" — nothing was inferred).
    "choice.set": ("set_choice", None),
    "choice.add": ("add_choice", None),
    "choice.remove": ("remove_choice", None),
    # arg: {from_word_index} — a HINT marking where dictated diagnosis
    # text begins. Deliberately NOT a code selection: no ICD-10 is ever
    # chosen by voice (sprint 13 scope).
    "diagnosis.capture": ("mark_diagnosis_text", None),
}


def operations_for(slot: CommandSlot) -> Operation:
    """Translate one CommandSlot into a single Operation.

    Section commands (``section.<name>``) carry their section_id in
    ``slot.arg``; we pass it through.
    """
    intent = slot.intent
    if intent.startswith("section."):
        return Operation(op="navigate_section", arg=slot.arg or {})
    # Sprint 13: a command whose argument could not be resolved carries a
    # ``reason`` instead of a value. It becomes the same no-op the FE
    # already knows how to surface, with a precise reason to toast —
    # never a guessed selection.
    if slot.arg and "reason" in slot.arg:
        return Operation(op="unknown_intent", arg={"intent": intent, **slot.arg})
    if intent not in _TABLE:
        # Unknown intent — return a no-op marker so the frontend can
        # surface a UI warning rather than guessing.
        return Operation(op="unknown_intent", arg={"intent": intent})
    op_name, arg = _TABLE[intent]
    if slot.arg:
        merged: dict[str, str] = dict(arg or {})
        merged.update(slot.arg)
        return Operation(op=op_name, arg=merged)
    return Operation(op=op_name, arg=arg)


KNOWN_INTENTS: frozenset[str] = frozenset({*_TABLE.keys(), "section"})  # "section.*"
