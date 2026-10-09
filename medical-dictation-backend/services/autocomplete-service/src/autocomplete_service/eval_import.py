"""CSV import for eval replicas — corpus-v2 §6.

Corpus v2 adds 86 lines (30 uk + 56 en). Typing them into a form one at a
time is not a plan, and the previous way to grow the corpus — editing
``eval_script.py`` — is what kept it at 34 lines for a sprint. So the file
IS the interface: the same spreadsheet the linguist writes in goes straight
in.

THREE PROPERTIES, in the order they matter:

  Preview before commit. A dry run answers "what would this do" —
  how many rows land, which are refused and why — with nothing written. The
  operator sees the damage before it happens, not in the audit log after.

  Atomic. The import is one transaction: 86 rows or none. A file that fails
  on row 60 leaves no half-corpus behind, which is what makes retrying it
  safe.

  Idempotent by id. Re-importing the same file skips every row it already
  added rather than allocating fresh ids for duplicates — the failure mode
  that turns "I'll just run it again to be sure" into a corpus with two of
  everything.

THE ID COMES FROM THE FILE, unlike console authoring where the server
allocates one. That is what makes idempotency possible at all, and it is why
the id is validated hard: it becomes a directory name in the exported
corpus.

WHY dataset='test' NEEDS A SEPARATE CONFIRMATION. v1 is frozen as the
holdout (§1.2). An import that could silently write into it would make every
tuning pass a quiet contamination of the test set — so `set=test` is refused
unless the caller says so explicitly, in the request, on purpose.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from . import eval_goldlint, eval_pii
from .eval_script import CONDITIONS, SUBSETS

#: §6's category vocabulary → the corpus's adversarial subsets. The CSV uses
#: short names because a human types them; storage uses the subset names the
#: rest of the pipeline already reports by. `base` is not a subset — a
#: baseline line is subset NULL, which is a real answer (eval_lines.validate).
CATEGORY_TO_SUBSET: Final[dict[str, str | None]] = {
    "base": None,
    "numbers": "numbers_doses_units",
    "drugs": "drug_names",
    "abbrev": "abbreviations",
    "codesw": "code_switching",
    "commands": "voice_commands",
    "noisy": "phone_mic_noisy",
}

#: §6 offers two conditions; the schema has four. `phone_noise` maps to
#: phone-speaker-distance — the sprint-18 dependency and the harsher of the
#: two — and the schema's own spellings are accepted unchanged so a file
#: written against the API is not refused for being precise.
CONDITION_ALIASES: Final[dict[str, str]] = {
    "headset": "headset",
    "phone_noise": "phone-speaker-distance",
    "phone": "phone-speaker-distance",
    "noise": "noisy",
    **{c: c for c in CONDITIONS},
}

DATASETS: Final[tuple[str, ...]] = ("dev", "test")
LANGUAGES: Final[tuple[str, ...]] = ("uk", "en", "de")

REQUIRED_COLUMNS: Final[tuple[str, ...]] = (
    "id",
    "lang",
    "category",
    "condition",
    "set",
    "text",
)
#: `paired` (Epic D) is a CSV-level opt-in: "record this one in BOTH
#: conditions". Accepts the spellings a human types in a spreadsheet.
OPTIONAL_COLUMNS: Final[tuple[str, ...]] = ("specialty", "transcript", "paired")

TRUTHY: Final[frozenset[str]] = frozenset(
    {"1", "true", "yes", "y", "так", "+", "paired"}
)

#: §6: "непорожній, ≤ 200 символів". Tighter than the 500 the column
#: allows — a replica is one utterance a person reads in one breath.
MAX_TEXT: Final = 200
DEFAULT_SPECIALTY: Final = "general"

_ID_RE: Final = re.compile(r"^[a-z0-9][a-z0-9_-]{0,119}$")


class CsvFormatError(ValueError):
    """The file cannot be read as the §6 format at all."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ImportRow:
    line_no: int
    script_id: str
    language: str
    subset: str | None
    category: str
    condition: str
    dataset: str
    say: str
    transcript: str
    specialty: str
    paired: bool = False


@dataclass(frozen=True, slots=True)
class Rejection:
    line_no: int
    script_id: str
    code: str
    field: str


@dataclass(slots=True)
class ParsedFile:
    rows: list[ImportRow] = field(default_factory=list)
    rejected: list[Rejection] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.rows) + len(self.rejected)


def decode(data: bytes) -> str:
    """UTF-8 with or without the BOM Excel insists on.

    §6 specifies BOM so Cyrillic opens correctly in Excel; ``utf-8-sig``
    accepts both, and a file saved by anything else fails loudly here rather
    than silently importing mojibake as gold text.
    """
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CsvFormatError("not_utf8", str(exc)) from None


def parse(text: str, *, allow_test: bool = False) -> ParsedFile:
    """Validate every row independently; never abort on the first bad one.

    A file is a batch of work, and "row 41 has a bad category" is only
    actionable next to the other 85 verdicts. The only whole-file failures
    are the ones that make the rest meaningless: wrong delimiter, missing
    columns.
    """
    if not text.strip():
        raise CsvFormatError("empty_file")

    first_line = text.splitlines()[0]
    if ";" in first_line and "," not in first_line:
        raise CsvFormatError("bad_delimiter", "expected comma-separated values")

    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise CsvFormatError("bad_header")
    header = [(name or "").strip().lower() for name in reader.fieldnames]
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        raise CsvFormatError("missing_columns", ",".join(missing))

    result = ParsedFile()
    seen_ids: set[str] = set()
    seen_text: dict[str, str] = {}

    for offset, raw in enumerate(reader):
        # +2: the header is line 1 and DictReader is zero-based.
        line_no = offset + 2
        row = {
            (k or "").strip().lower(): (v or "").strip()
            for k, v in raw.items()
            if k is not None
        }
        script_id = row.get("id", "")
        rejection = _validate(row, script_id, line_no, seen_ids, allow_test=allow_test)
        if rejection is not None:
            result.rejected.append(rejection)
            continue

        language = row["lang"]
        category = row["category"]
        body = row["text"]
        gold = row.get("transcript") or body
        seen_ids.add(script_id)

        # Epic B: the gold this row would store, held against the style
        # guide. A warning and a proposal, never a rejection — the importer's
        # job is to get 86 lines in, and "пульс 68/хв" is a real utterance
        # somebody typed in good faith. The suggestion is what makes the
        # rule cheap enough to follow.
        lint = eval_goldlint.lint_gold(say=body, transcript=gold, language=language)
        if not lint.clean:
            result.warnings.append(
                {
                    "code": "gold_style",
                    "script_id": script_id,
                    "findings": [f.code for f in lint.findings],
                    "suggestion": lint.suggestion,
                }
            )

        # A duplicated text is usually a copy-paste that was meant to be
        # edited — worth saying, never worth refusing: two conditions
        # recording the same sentence is a legitimate design (§1.2).
        if body in seen_text:
            result.warnings.append(
                {
                    "code": "duplicate_text",
                    "script_id": script_id,
                    "same_as": seen_text[body],
                }
            )
        else:
            seen_text[body] = script_id

        result.rows.append(
            ImportRow(
                line_no=line_no,
                script_id=script_id,
                language=language,
                subset=CATEGORY_TO_SUBSET[category],
                category=category,
                condition=CONDITION_ALIASES[row["condition"]],
                dataset=row.get("set") or "dev",
                say=body,
                transcript=gold,
                specialty=row.get("specialty") or DEFAULT_SPECIALTY,
                paired=row.get("paired", "").strip().lower() in TRUTHY,
            )
        )
    return result


def _validate(
    row: dict[str, str],
    script_id: str,
    line_no: int,
    seen_ids: set[str],
    *,
    allow_test: bool,
) -> Rejection | None:
    def reject(code: str, field_name: str) -> Rejection:
        return Rejection(line_no=line_no, script_id=script_id, code=code, field=field_name)

    if not script_id or not _ID_RE.match(script_id):
        return reject("bad_id", "id")
    if script_id in seen_ids:
        return reject("duplicate_id_in_file", "id")

    language = row.get("lang", "")
    if language not in LANGUAGES:
        return reject("unknown_language", "lang")
    # The §6 id template is "{lang}-{category}-{NNN}". Only the language
    # half is enforced: it is the part that silently produces a corpus where
    # a Ukrainian line is scored with an English hint.
    if not script_id.startswith(f"{language}-"):
        return reject("id_language_mismatch", "id")

    category = row.get("category", "")
    if category not in CATEGORY_TO_SUBSET:
        return reject("unknown_category", "category")
    if category not in SUBSETS and CATEGORY_TO_SUBSET[category] not in (None, *SUBSETS):
        return reject("unknown_category", "category")

    if row.get("condition", "") not in CONDITION_ALIASES:
        return reject("unknown_condition", "condition")

    dataset = row.get("set") or "dev"
    if dataset not in DATASETS:
        return reject("unknown_set", "set")
    if dataset == "test" and not allow_test:
        # The holdout guard. Not an error in the file — a refusal to write
        # into the frozen set without being told to (§1.2, §6).
        return reject("test_requires_confirmation", "set")

    body = row.get("text", "")
    if not body or len(body) > MAX_TEXT:
        return reject("bad_text", "text")
    gold = row.get("transcript") or ""
    if gold and len(gold) > MAX_TEXT:
        return reject("bad_text", "transcript")

    specialty = row.get("specialty") or DEFAULT_SPECIALTY
    if not specialty or len(specialty) > 64:
        return reject("bad_specialty", "specialty")

    # The same sweep the authoring routes run: PII must never become a row,
    # whichever door it arrives through.
    if eval_pii.sweep(body) or (gold and eval_pii.sweep(gold)):
        return reject("pii_detected", "text")

    return None


def coverage_matrix(
    existing: list[dict[str, Any]], pending: Sequence[ImportRow] = ()
) -> list[dict[str, Any]]:
    """Utterances per (dataset, language, subset), with pending rows folded in.

    On a dry run ``pending`` is what WOULD be added, so the operator sees the
    coverage the commit would produce rather than the one it starts from —
    the number §1.2's design table is read against.
    """
    counts: dict[tuple[str, str, str], int] = {}
    for row in existing:
        key = (str(row["dataset"]), str(row["language"]), str(row["subset"]))
        counts[key] = counts.get(key, 0) + int(row["utterances"])
    for item in pending:
        key = (item.dataset, item.language, item.subset or "baseline")
        counts[key] = counts.get(key, 0) + 1
    return [
        {"dataset": d, "language": lang, "subset": subset, "utterances": n}
        for (d, lang, subset), n in sorted(counts.items())
    ]
