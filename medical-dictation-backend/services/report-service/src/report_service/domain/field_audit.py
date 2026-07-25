"""Confirm/override audit signals for typed fields (sprint 13, step 06).

These two events are the extractor's quality feedback loop: how often
clinicians accept a proposal versus replace it. Step 08's dashboard
reads them; a rising override rate means alias coverage or the
threshold needs work — data, not intuition.

**The PHI line.** Payloads carry the section key, the field type, and
— only for CLOSED vocabularies — the option slug or ICD-10 code.
Free-text values are never included. The audit chain is append-only
and hash-linked: a "changed it to <prose>" payload would put
unerasable PHI in it. Enforced by tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from report_models import ReportContent

# Field types whose values are closed vocabularies and therefore safe to
# record. Anything else contributes its type only.
_CLOSED_VOCABULARY: frozenset[str] = frozenset({"choice", "multi_choice", "structured_diagnosis"})


@dataclass(frozen=True, slots=True)
class FieldAuditEvent:
    kind: Literal["confirmed", "overridden"]
    section_key: str
    field_type: str
    payload: dict[str, Any]


def _selected(metadata: dict[str, Any]) -> tuple[str, ...]:
    raw = metadata.get("selected")
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, list | tuple):
        return tuple(str(v) for v in raw)
    return ()


def _codes(section: Any) -> tuple[str, ...]:
    return tuple(sorted(str(c.code) for c in getattr(section, "icd10", []) or []))


def diff_field_events(
    *,
    before: ReportContent | None,
    after: ReportContent,
    field_types: dict[str, str],
) -> list[FieldAuditEvent]:
    """Compare two draft versions and describe what the clinician did.

    ``field_types`` maps section_key → field_type (from the template).

    - **confirmed**: an ``extracted`` value became ``manual`` with the
      same content, or a proposal's code entered ``section.icd10``.
    - **overridden**: a ``manual`` write replaced an extracted value
      with a DIFFERENT one. This is the signal that matters — it says
      the extractor was wrong.
    """
    events: list[FieldAuditEvent] = []
    old_by_key = {s.section_key: s for s in (before.sections if before else [])}

    for section in after.sections:
        field_type = field_types.get(section.section_key, "free_text")
        old = old_by_key.get(section.section_key)
        old_meta: dict[str, Any] = dict(getattr(old, "field_specific_metadata", {}) or {})
        new_meta: dict[str, Any] = dict(section.field_specific_metadata or {})

        safe = field_type in _CLOSED_VOCABULARY

        if field_type == "structured_diagnosis":
            old_codes, new_codes = _codes(old), _codes(section)
            gained = tuple(c for c in new_codes if c not in old_codes)
            if gained:
                proposed = {
                    str(p.get("code")) for p in old_meta.get("proposals", []) if isinstance(p, dict)
                }
                # A code that was proposed and is now confirmed = accepted.
                # A code that was never proposed = the clinician's own.
                accepted = tuple(c for c in gained if c in proposed)
                authored = tuple(c for c in gained if c not in proposed)
                if accepted:
                    events.append(
                        FieldAuditEvent(
                            kind="confirmed",
                            section_key=section.section_key,
                            field_type=field_type,
                            payload={"codes": list(accepted)},
                        )
                    )
                if authored and proposed:
                    # Proposals existed and the clinician picked something
                    # else — the strongest "we were wrong" signal there is.
                    events.append(
                        FieldAuditEvent(
                            kind="overridden",
                            section_key=section.section_key,
                            field_type=field_type,
                            payload={
                                "codes": list(authored),
                                "proposed": sorted(proposed),
                            },
                        )
                    )
            continue

        if not old_meta and not new_meta:
            continue

        old_source = old_meta.get("source")
        new_source = new_meta.get("source")
        if new_source != "manual":
            continue

        old_values, new_values = _selected(old_meta), _selected(new_meta)
        if old_source == "extracted":
            same = old_values == new_values
            kind: Literal["confirmed", "overridden"] = "confirmed" if same else "overridden"
            payload: dict[str, Any] = {}
            if safe:
                payload["selected"] = list(new_values)
                if not same:
                    payload["was"] = list(old_values)
            events.append(
                FieldAuditEvent(
                    kind=kind,
                    section_key=section.section_key,
                    field_type=field_type,
                    payload=payload,
                )
            )
    return events
