"""The honest fallback (rule SC3: `insufficient_basis` is a designed answer
state, not an error).

Composed by code, never by a model. A model asked to explain why it has
nothing to say will write a fluent paragraph that reads like an answer, which
is the precise failure this state exists to prevent.

The segments are `missing_info` and `next_step` — never `evidence` (there is
none) and never `interpretation` (there is nothing to interpret).
"""

from __future__ import annotations

from evidence_models import Flag, FlagSeverity, Segment, SegmentKind

# reason code -> (uk, en) missing-info text
_REASONS: dict[str, tuple[str, str]] = {
    "no_passages": (
        "У доступному корпусі та в дозволених вебджерелах не знайдено матеріалів, "
        "які відповідають на це запитання.",
        "Nothing in the available corpus or the allowlisted web sources addresses this question.",
    ),
    "binder_failed": (
        "Відповідь не вдалося сформувати так, щоб кожне твердження спиралося на "
        "конкретне джерело, тому вона не показується.",
        "An answer could not be produced in which every statement is tied to a "
        "specific source, so none is shown.",
    ),
    "synthesis_unavailable": (
        "Сервіс генерації відповіді зараз недоступний.",
        "The answer generation service is currently unavailable.",
    ),
    "retrieval_unavailable": (
        "Сервіс пошуку доказів зараз недоступний.",
        "The evidence retrieval service is currently unavailable.",
    ),
}

_NEXT_STEP: dict[str, tuple[str, str]] = {
    "no_passages": (
        "Спробуйте переформулювати запитання конкретніше або зверніться до "
        "первинного джерела настанови.",
        "Try a more specific phrasing, or consult the primary guideline directly.",
    ),
    "binder_failed": (
        "Спробуйте поставити запитання ще раз; якщо це повториться, повідомте "
        "адміністратора знань.",
        "Try asking again; if this repeats, report it to your knowledge admin.",
    ),
    "synthesis_unavailable": (
        "Спробуйте пізніше.",
        "Try again shortly.",
    ),
    "retrieval_unavailable": (
        "Спробуйте пізніше.",
        "Try again shortly.",
    ),
}


def _localized(table: dict[str, tuple[str, str]], reason: str, locale: str) -> str:
    uk, en = table.get(reason, table["no_passages"])
    return uk if locale.split("-")[0].casefold() == "uk" else en


def compose(reason: str, *, locale: str) -> tuple[list[Segment], list[Flag]]:
    """Returns (summary_segments, flags) for an `insufficient_basis` envelope."""
    segments = [
        Segment(
            id="seg-1",
            kind=SegmentKind.missing_info,
            text=_localized(_REASONS, reason, locale),
        ),
        Segment(
            id="seg-2",
            kind=SegmentKind.next_step,
            text=_localized(_NEXT_STEP, reason, locale),
        ),
    ]
    flags = [
        Flag(
            code="insufficient_basis",
            severity=FlagSeverity.info,
            message=reason,
        )
    ]
    return segments, flags
