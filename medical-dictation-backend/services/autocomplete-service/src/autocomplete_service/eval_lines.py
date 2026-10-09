"""One script, two halves — the vendored spine and the tenant's own lines.

``eval_script.SCRIPT`` is the corpus the repo ships: 34 synthetic lines
covering the six adversarial subsets. It is read-only by construction (it
lives in a Python module) and it is the same for every tenant.

Migration 0091 adds the other half: lines a tenant authored from the
console, with the same category and labels the vendored ones carry —
language, specialty, subset, suggested condition, spoken form, gold text.
This module is the seam. Everything that asks "what may be recorded?"
(the script view, the take-upload guard, the publisher) asks it here, so
the two halves can never drift into two different answers.

WHAT DOES NOT CHANGE: the scripted-only invariant. A take must still name a
line that exists server-side, and the stored gold text is still the
SERVER's copy. Authoring adds a way to create a line — deliberately, with a
category, a PII sweep and (for ad-hoc capture) an attestation — not a way
to skip having one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import asyncpg

from . import eval_repository as eval_repo
from .eval_script import (
    CONDITIONS,
    ROW_BY_ID,
    SCRIPT,
    SCRIPT_VERSION,
    SUBSETS,
    gold_transcript,
)

__all__ = [
    "ADHOC",
    "AUTHORED",
    "BUILTIN",
    "DATASETS",
    "DEV",
    "TEST",
    "LANGUAGES",
    "MAX_SPECIALTY",
    "MAX_TEXT",
    "ROW_BY_ID",
    "SCRIPT_VERSION",
    "FieldError",
    "Line",
    "all_lines",
    "allocate_script_id",
    "resolve",
    "slug",
    "validate",
]

BUILTIN: str = "builtin"
AUTHORED: str = "authored"
ADHOC: str = "adhoc"

#: The two halves of the corpus (0092). New lines default to DEV; the
#: vendored spine and everything recorded before the split are TEST.
DEV: str = "dev"
TEST: str = "test"
DATASETS: tuple[str, ...] = (DEV, TEST)

MAX_TEXT = 500
MAX_SPECIALTY = 64
LANGUAGES: tuple[str, ...] = ("uk", "en", "de")

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class Line:
    """A recordable line, whichever half it came from."""

    script_id: str
    language: str
    specialty: str
    subset: str | None
    say: str
    transcript: str
    condition: str | None
    source: str  # builtin | authored | adhoc
    # dev = tunable, test = the frozen holdout (0092). Vendored lines are the
    # v1 corpus and are therefore always 'test' — the split exists so that
    # tuning against dev cannot quietly become tuning against the test set.
    dataset: str = TEST
    # Part of the paired design (0095, Epic D): this line is recorded in BOTH
    # conditions so the two can be compared on the same text. Vendored lines
    # are not paired — the paired subset is a decision about how a corpus is
    # used, and the tenant makes it.
    paired: bool = False

    @property
    def editable(self) -> bool:
        """Vendored lines are immutable through the API. Changing one would
        change what a previously exported corpus claims was read aloud."""
        return self.source != BUILTIN


def _builtin_line(row: dict[str, Any]) -> Line:
    return Line(
        script_id=row["id"],
        language=row["language"],
        specialty=row["specialty"],
        subset=row.get("subset"),
        say=row["say"],
        transcript=gold_transcript(row),
        condition=row.get("condition"),
        source=BUILTIN,
        dataset=TEST,
        paired=False,
    )


def _item_line(rec: asyncpg.Record) -> Line:
    return Line(
        script_id=rec["script_id"],
        language=rec["language"],
        specialty=rec["specialty"],
        subset=rec["subset"],
        say=rec["say"],
        transcript=rec["transcript"],
        condition=rec["condition"],
        source=rec["source"],
        dataset=rec["dataset"],
        paired=rec["paired"],
    )


async def all_lines(conn: asyncpg.Connection) -> list[Line]:
    """Vendored lines first, then the tenant's own in creation order. The
    order is the worklist's order, and putting new lines at the end keeps a
    half-finished labelling job's positions stable."""
    authored = [_item_line(r) for r in await eval_repo.list_script_items(conn)]
    return [_builtin_line(r) for r in SCRIPT] + authored


async def resolve(conn: asyncpg.Connection, script_id: str) -> Line | None:
    """The one line a take may be filed under, or None.

    Vendored ids win: a tenant cannot shadow a repo line with one of its own
    (the id allocator refuses those ids, and this order makes it true even
    if a row somehow existed).
    """
    builtin = ROW_BY_ID.get(script_id)
    if builtin is not None:
        return _builtin_line(builtin)
    rec = await eval_repo.get_script_item(conn, script_id)
    return _item_line(rec) if rec is not None else None


def slug(text: str) -> str:
    """A specialty as it appears inside a script id — and inside the corpus
    directory path that id becomes."""
    return _SLUG_STRIP.sub("-", text.strip().lower()).strip("-") or "general"


async def allocate_script_id(
    conn: asyncpg.Connection, *, language: str, specialty: str
) -> str:
    """``uk-cardiology-a001`` — same shape as the vendored ids so the corpus
    layout stays readable, with an ``a`` marking it authored so no generated
    id can ever collide with a future vendored one.

    Scans the tenant's existing ids under the prefix rather than counting
    rows: a deleted line must not hand its number to the next one, or two
    different recordings end up with the same utterance id in two exports.
    """
    prefix = f"{language}-{slug(specialty)}-a"
    taken = await eval_repo.taken_script_ids(conn, prefix)
    n = 1
    while f"{prefix}{n:03d}" in taken or f"{prefix}{n:03d}" in ROW_BY_ID:
        n += 1
    return f"{prefix}{n:03d}"


@dataclass(frozen=True, slots=True)
class FieldError:
    field: str
    code: str


def validate(
    *,
    language: str,
    specialty: str,
    subset: str | None,
    say: str,
    transcript: str | None,
    condition: str | None,
    dataset: str | None = None,
    paired: bool = False,
) -> list[FieldError]:
    """Field-shape checks shared by create, edit and ad-hoc capture.

    The subset vocabulary is closed on purpose. The six adversarial subsets
    are what the corpus measures (numbers, drugs, abbreviations, code
    switching, voice commands, phone-mic noise); a free-text seventh would
    produce a bucket with two utterances in it that nobody can read a WER
    from. A line that fits none of them is a baseline line — subset null —
    and that is a real answer, not a fallback.
    """
    errors: list[FieldError] = []
    if language not in LANGUAGES:
        errors.append(FieldError("language", "unknown_language"))
    if not specialty.strip() or len(specialty) > MAX_SPECIALTY:
        errors.append(FieldError("specialty", "bad_specialty"))
    if subset is not None and subset not in SUBSETS:
        errors.append(FieldError("subset", "unknown_subset"))
    if condition is not None and condition not in CONDITIONS:
        errors.append(FieldError("condition", "unknown_condition"))
    if not say.strip() or len(say) > MAX_TEXT:
        errors.append(FieldError("say", "bad_text"))
    if transcript is not None and (not transcript.strip() or len(transcript) > MAX_TEXT):
        errors.append(FieldError("transcript", "bad_text"))
    if dataset is not None and dataset not in DATASETS:
        errors.append(FieldError("dataset", "unknown_dataset"))
    # A paired line is recorded in BOTH conditions, so "which condition" has
    # to have an answer before "and also the other one" means anything. The
    # pair is (line.condition, the other one the design uses).
    if paired and condition is None:
        errors.append(FieldError("condition", "paired_requires_condition"))
    return errors
