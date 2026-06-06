"""Operations dispatch — map a CommandSlot's intent → frontend Operation.

The frontend (sprint 04 + 06) consumes Operations to mutate editor
state. This module is the contract between intents (linguistic) and
operations (UI-side). Adding a new intent without an op is a bug; the
test suite enforces a 1:1 mapping.
"""

from __future__ import annotations

from ..pipeline.base import CommandSlot, Operation

_TABLE: dict[str, tuple[str, dict[str, str] | None]] = {
    "newparagraph":   ("insert_paragraph_break", None),
    "newline":        ("insert_line_break", None),
    "period":         ("insert_punctuation", {"value": "."}),
    "comma":          ("insert_punctuation", {"value": ","}),
    "question_mark":  ("insert_punctuation", {"value": "?"}),
    "save_draft":     ("save_draft", None),
    "undo_last":      ("undo_last", None),
    "stop_dictation": ("stop_dictation", None),
    "begin_quote":    ("insert_quote_marker", {"value": "open"}),
    "end_quote":      ("insert_quote_marker", {"value": "close"}),
    "insert_template":("insert_template", None),
}


def operations_for(slot: CommandSlot) -> Operation:
    """Translate one CommandSlot into a single Operation.

    Section commands (``section.<name>``) carry their section_id in
    ``slot.arg``; we pass it through.
    """
    intent = slot.intent
    if intent.startswith("section."):
        return Operation(op="navigate_section", arg=slot.arg or {})
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
