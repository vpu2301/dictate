"""Pre-transition finalize validation.

Ensures every section flagged ``required`` in the template has content
≥ ``min_chars``, and ICD-10 is present where ``icd10_required`` is
true.

Sprint 13 adds **typed** completeness: ``min_chars`` measures prose, so
it says nothing about a ``choice`` section whose answer lives in
``field_specific_metadata``. Each typed field type gets its own
"filled" rule (see ``_typed_problem``).

The authority rule is absolute: a ``structured_diagnosis`` section is
filled only by CONFIRMED codes in ``section.icd10``. Extractor
``proposals`` never count, under any configuration — a finalized,
signable report must never carry a machine-chosen diagnosis.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from uuid import UUID

from report_models import ReportContent
from template_models import TemplateDefinition

# Normalised, contract-facing reasons keyed by the legacy ``code``.
# Anything unmapped falls through to the code itself.
_REASON_BY_CODE: Final[dict[str, str]] = {
    "missing_required_section": "required_empty",
    "below_min_chars": "below_min_chars",
    "missing_icd10": "missing_icd10",
    "missing_patient": "patient_required",
    # Sprint 13 — typed completeness.
    "choice_not_selected": "choice_not_selected",
    "numeric_not_filled": "numeric_not_filled",
    "date_not_filled": "date_not_filled",
    # Distinct from missing_icd10 so the FE can say "confirm the
    # proposed diagnosis" rather than "enter a diagnosis".
    "diagnosis_not_confirmed": "diagnosis_not_confirmed",
}

# Field types whose completeness lives in metadata, not in prose.
_TYPED_FIELD_TYPES: Final[frozenset[str]] = frozenset(
    {
        "choice",
        "multi_choice",
        "numeric_with_unit",
        "date",
        "date_with_note",
        "structured_diagnosis",
    }
)


@dataclass(slots=True)
class FinalizeProblem:
    field: str
    code: str
    detail: str
    section_key: str | None = None

    @property
    def reason(self) -> str:
        return _REASON_BY_CODE.get(self.code, self.code)

    def as_dict(self) -> dict[str, str | None]:
        # Legacy keys (field/code/detail) retained for backward compat;
        # section_key + reason added for the aligned finalize contract.
        return {
            "field": self.field,
            "code": self.code,
            "detail": self.detail,
            "section_key": self.section_key,
            "reason": self.reason,
        }


def _field_type_of(tpl_section: object) -> str:
    """The section's field type as a plain string.

    ``TemplateDefinition`` carries a ``FieldType`` StrEnum, but the
    sprint-08 tests (and any legacy ad-hoc template) pass duck-typed
    objects without one — those default to free_text and keep their
    sprint-08 behaviour exactly.
    """
    raw = getattr(tpl_section, "field_type", "free_text")
    return str(getattr(raw, "value", raw))


def _typed_problem(
    *,
    field_type: str,
    section_key: str,
    body: object | None,
    require_confirmed_diagnosis: bool,
) -> FinalizeProblem | None:
    """Typed "filled" check for one required section. ``None`` ⇒ filled."""
    metadata: dict[str, object] = dict(getattr(body, "field_specific_metadata", {}) or {})

    if field_type in {"choice", "multi_choice"}:
        selected = metadata.get("selected")
        # Any source counts: the clinician saw the proposal and chose to
        # finalize, which is acceptance. Diagnoses are the exception.
        if selected in (None, "", [], ()):
            return FinalizeProblem(
                field=f"sections.{section_key}.field_specific_metadata.selected",
                code="choice_not_selected",
                detail=f"section {section_key!r} requires a selection",
                section_key=section_key,
            )
        return None

    if field_type == "numeric_with_unit":
        if metadata.get("value") is None or not metadata.get("unit"):
            return FinalizeProblem(
                field=f"sections.{section_key}.field_specific_metadata.value",
                code="numeric_not_filled",
                detail=f"section {section_key!r} requires a value and a unit",
                section_key=section_key,
            )
        return None

    if field_type in {"date", "date_with_note"}:
        if not metadata.get("date"):
            return FinalizeProblem(
                field=f"sections.{section_key}.field_specific_metadata.date",
                code="date_not_filled",
                detail=f"section {section_key!r} requires a date",
                section_key=section_key,
            )
        return None

    if field_type == "structured_diagnosis":
        confirmed = list(getattr(body, "icd10", []) or [])
        if confirmed:
            return None
        # Proposals exist but nothing is confirmed → a DIFFERENT problem
        # from "no diagnosis at all": the clinician has one tap to make.
        has_proposals = bool(metadata.get("proposals"))
        if has_proposals and require_confirmed_diagnosis:
            return FinalizeProblem(
                field=f"sections.{section_key}.icd10",
                code="diagnosis_not_confirmed",
                detail=(
                    f"section {section_key!r} has proposed ICD-10 codes that "
                    "must be confirmed before finalize"
                ),
                section_key=section_key,
            )
        return FinalizeProblem(
            field=f"sections.{section_key}.icd10",
            code="missing_icd10",
            detail=f"section {section_key!r} requires at least one confirmed ICD-10 code",
            section_key=section_key,
        )

    return None


def validate_finalize(
    *,
    content: ReportContent,
    template: TemplateDefinition,
    patient_id: UUID | None,
    require_confirmed_diagnosis: bool = True,
) -> list[FinalizeProblem]:
    problems: list[FinalizeProblem] = []

    # Every finalized report must reference a patient. The API + DB
    # constraint (migration 0033) make a patient-less draft impossible to
    # create, but we re-assert it here as defence-in-depth for any legacy
    # draft that slipped through before the invariant existed.
    if patient_id is None:
        problems.append(
            FinalizeProblem(
                field="patient_id",
                code="missing_patient",
                detail="report must reference a patient before finalize",
            )
        )

    by_key = {s.section_key: s for s in content.sections}
    for tpl_section in template.sections:
        # TemplateSection's key field is ``id`` (template_models.schema);
        # older ad-hoc definitions used ``key`` — accept both.
        section_key = getattr(tpl_section, "key", None) or tpl_section.id
        body = by_key.get(section_key)
        required = bool(getattr(tpl_section, "required", False))
        min_chars = int(getattr(tpl_section, "min_chars", 0) or 0)

        field_type = _field_type_of(tpl_section)

        # ── Sprint 13: typed sections measure completeness in metadata ──
        if field_type in _TYPED_FIELD_TYPES:
            if required:
                problem = _typed_problem(
                    field_type=field_type,
                    section_key=section_key,
                    body=body,
                    require_confirmed_diagnosis=require_confirmed_diagnosis,
                )
                if problem is not None:
                    problems.append(problem)
            # date_with_note additionally holds prose: the note IS content,
            # so min_chars still applies to it when the author set one.
            if (
                field_type == "date_with_note"
                and body is not None
                and min_chars > 0
                and len(body.text.strip()) < min_chars
            ):
                problems.append(
                    FinalizeProblem(
                        field=f"sections.{section_key}.text",
                        code="below_min_chars",
                        detail=f"section {section_key!r} needs at least {min_chars} chars",
                        section_key=section_key,
                    )
                )
            continue

        if required and (body is None or len(body.text.strip()) == 0):
            problems.append(
                FinalizeProblem(
                    field=f"sections.{section_key}.text",
                    code="missing_required_section",
                    detail=f"section {section_key!r} is required",
                    section_key=section_key,
                )
            )
            continue
        if body is not None and min_chars > 0 and len(body.text.strip()) < min_chars:
            problems.append(
                FinalizeProblem(
                    field=f"sections.{section_key}.text",
                    code="below_min_chars",
                    detail=f"section {section_key!r} needs at least {min_chars} chars",
                    section_key=section_key,
                )
            )
        if getattr(tpl_section, "icd10_required", False):
            has_icd = bool(body and body.icd10) or bool(content.icd10_codes)
            if not has_icd:
                problems.append(
                    FinalizeProblem(
                        field=f"sections.{section_key}.icd10",
                        code="missing_icd10",
                        detail=f"section {section_key!r} requires at least one ICD-10 code",
                        section_key=section_key,
                    )
                )
    return problems
