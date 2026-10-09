"""Recording instructions, assembled server-side — corpus-v3 Epic C.

THE PROBLEM THIS SOLVES is not "the recordist did not know what to do". It
is that nothing in the system could tell whether they had done it. The
condition was a session-wide dropdown, so a replica marked `phone_noise`
could be, and sometimes was, recorded on a headset in a quiet room — and the
per-condition WER table in the report then compared labels rather than
setups (Epic D has the numbers).

So the condition moves onto the REPLICA, and the instruction that goes with
it is composed here from two sentences:

    condition  what setup to use   ("Телефон на відстані близько метра…")
  + category   what to watch for   ("Числа промовляйте чітко…")

WHY TWO ROWS AND NOT ONE PER COMBINATION. Four conditions × seven categories
is twenty-eight strings to keep consistent, and the failure mode of that
table is not that a cell is missing — it is that twenty-six of them get
updated and two do not. Composing from 4 + 7 makes the inconsistency
impossible to express.

WHY THE SERVER OWNS THE TEXT. The recording protocol IS the measurement
condition. A copy in the SPA would be a second protocol, drifting, and the
one that actually gets read is the one nobody reviews. The console renders
whatever this serves and hardcodes nothing.

The tenant may override either half (migration 0094); ``compose`` never sees
that distinction because the repository already resolved it.
"""

from __future__ import annotations

from typing import Any, Final

__all__ = [
    "BASELINE",
    "DEFAULT_LANG",
    "Instruction",
    "compose",
    "index",
]

#: A line with no subset is a baseline line — a real answer, not a gap, so
#: it gets a real instruction rather than falling through to silence.
BASELINE: Final = "baseline"

#: The console's UI language. Ukrainian is the only seeded set; anything
#: else falls back to it rather than serving an empty banner, because a
#: recordist with no instruction is the state Epic C exists to remove.
DEFAULT_LANG: Final = "uk"


class Instruction:
    """The finished text plus the two halves it came from.

    The halves are exposed because the console highlights the condition
    sentence differently for `phone-speaker-distance` (Epic C's "контрастне
    тло, щоб не пропустити") and cannot do that with one concatenated
    string.
    """

    __slots__ = ("category_text", "condition_text")

    def __init__(self, condition_text: str, category_text: str) -> None:
        self.condition_text = condition_text
        self.category_text = category_text

    @property
    def text(self) -> str:
        return " ".join(p for p in (self.condition_text, self.category_text) if p)

    def as_dict(self) -> dict[str, str]:
        return {
            "text": self.text,
            "condition_text": self.condition_text,
            "category_text": self.category_text,
        }


def index(rows: list[Any]) -> tuple[dict[str, str], dict[str, str]]:
    """Split the template rows into (by condition, by category)."""
    by_condition: dict[str, str] = {}
    by_category: dict[str, str] = {}
    for row in rows:
        if row["condition"]:
            by_condition[str(row["condition"])] = str(row["text"])
        elif row["category"]:
            by_category[str(row["category"])] = str(row["text"])
    return by_condition, by_category


def compose(
    *,
    condition: str | None,
    subset: str | None,
    by_condition: dict[str, str],
    by_category: dict[str, str],
) -> Instruction:
    """The instruction for one replica.

    A replica with no condition of its own gets no condition sentence — and
    that is honest rather than helpful: it means nobody decided how this
    line should be recorded, and inventing "use a headset" here would turn
    an open question into a silent default. The console shows the category
    half and the condition control unbound.
    """
    condition_text = by_condition.get(condition or "", "")
    category_text = by_category.get(subset or BASELINE, "")
    return Instruction(condition_text, category_text)
