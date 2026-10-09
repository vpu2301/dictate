#!/usr/bin/env python
"""BLOCKING CI gate: no email template may render PHI.

Renders every emailing category against a payload deliberately stuffed
with fake PHI — a Ukrainian patient name, an ІПН, a diagnosis, an ICD
code, a date of birth — and fails if any of those tokens survive into
the subject, the text body, or the HTML body.

Why this catches real regressions rather than restating the design:
the allow-list in `domain/render.ALLOWED_PAYLOAD_KEYS` is the control,
but a future edit that adds `patient_name` to a template, or widens an
allow-list entry "just for debugging", would be invisible in review.
This gate turns that edit into a red build.

It also fails if a category declares a template that does not exist, or
renders a template that leaves a Jinja placeholder unfilled.

Mirrors the sprint-07 privacy gate philosophy: assert the negative,
mechanically, on every push.

Exit 0 = clean. Exit 1 = a leak or a broken template.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services/notification-service/src"))
sys.path.insert(0, str(ROOT / "libs/notification_events/src"))

from datetime import UTC, datetime  # noqa: E402
from uuid import uuid4  # noqa: E402

from notification_events import Category, NotificationEvent  # noqa: E402
from notification_service.adapters.templates import render_email  # noqa: E402
from notification_service.domain.catalog import CATALOG, emailing_categories  # noqa: E402
from notification_service.domain.render import (  # noqa: E402
    ALLOWED_PAYLOAD_KEYS,
    deep_link,
    safe_payload,
)

# Tokens that must NEVER reach a rendered email. Deliberately realistic:
# a Cyrillic surname, a 10-digit ІПН, a Ukrainian diagnosis string, an
# ICD-10 code, and a DOB.
PHI_TOKENS: tuple[str, ...] = (
    "Іваненко",
    "Ivanenko",
    "3216549870",  # ІПН
    "цукровий діабет",
    "diabetes mellitus",
    "E11.9",  # ICD-10
    "1978-04-12",  # DOB
    "+380671234567",
)
# NOT tokens: bare common nouns like "пацієнт"/"patient". They occur in
# legitimate boilerplate ("не містить медичних даних пацієнта"), so a
# substring match on them fails every template and the gate gets muted
# — the classic way a security check stops being enforced. The PHI is
# the NAME, the ІПН and the diagnosis; `report_title` above is caught
# because it embeds "Іваненко".

# A payload a careless producer might send. Every PHI key here is
# expected to be dropped by the allow-list before rendering.
POISONED_PAYLOAD: dict[str, str | int | float | bool | None] = {
    # Legitimate, allow-listed keys — these SHOULD appear.
    "report_code": "RPT-2026-0042",
    "signature_level": "qualified",
    "failure_reason": "provider timeout",
    "check_name": "chain_reconciler",
    "shared_by_display": "Колега",
    "version": "3",
    "count": "5",
    "period": "день",
    # PHI a producer must never be able to surface.
    "patient_name": "Іваненко Петро Миколайович",
    "patient_name_en": "Ivanenko Petro",
    "ipn": "3216549870",
    "diagnosis": "цукровий діабет 2 типу",
    "diagnosis_en": "diabetes mellitus type 2",
    "icd10": "E11.9",
    "dob": "1978-04-12",
    "phone": "+380671234567",
    "report_title": "Пацієнт Іваненко — цукровий діабет",
}


def _event(category: Category) -> NotificationEvent:
    # The envelope itself rejects oversized/nested payloads; build it
    # through the real model so the gate exercises the real path.
    return NotificationEvent(
        event_id=uuid4(),
        tenant_id=uuid4(),
        category=category,
        actor_user_id=uuid4(),
        resource_type="report",
        resource_id=uuid4(),
        occurred_at=datetime.now(UTC),
        payload=POISONED_PAYLOAD,
    )


def main() -> int:
    failures: list[str] = []
    checked = 0

    categories = sorted(emailing_categories(), key=str)
    if not categories:
        print("FAIL: no emailing categories found — the gate would be vacuous")
        return 1

    for category in categories:
        spec = CATALOG[category]
        event = _event(category)
        fields = safe_payload(event)
        link = deep_link(event, base_url="https://app.example")

        try:
            rendered = render_email(
                category,
                template_stem=spec.email_template,
                fields=fields,
                deep_link=link,
                items=["Звіт RPT-2026-0042 завершено"],
            )
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{category}: template {spec.email_template!r} failed: {exc}")
            continue

        checked += 1
        surfaces = {
            "subject": rendered.subject,
            "text": rendered.text_body,
            "html": rendered.html_body,
        }

        for surface_name, content in surfaces.items():
            for token in PHI_TOKENS:
                if token.lower() in content.lower():
                    failures.append(
                        f"{category}: PHI token {token!r} leaked into the "
                        f"{surface_name} of template {spec.email_template!r}"
                    )
            # An unrendered placeholder means the template referenced
            # something the allow-list does not provide.
            if "{{" in content or "{%" in content:
                failures.append(
                    f"{category}: unrendered Jinja placeholder in {surface_name}"
                )

        # Actionability, checked against what this category is ABOUT.
        #
        # For a mail about a report, the code is the whole point: a
        # template that lost it is broken even though it leaks nothing.
        # For one that carries no resource pointer at all — S21's
        # `security.mfa_reminder` is about the recipient's own account,
        # and its allow-list is deliberately two non-identifying keys —
        # the equivalent is the link: a security ask with nothing to
        # click is a mail that cannot be acted on.
        body = rendered.subject + rendered.text_body
        if "report_code" in ALLOWED_PAYLOAD_KEYS.get(category, frozenset()):
            if "RPT-2026-0042" not in body:
                failures.append(
                    f"{category}: report_code missing from the rendered mail — "
                    "the pointer is what makes the notification actionable"
                )
        elif link not in rendered.text_body + rendered.html_body:
            failures.append(
                f"{category}: neither a resource pointer nor the deep link "
                "survived into the mail — nothing about it is actionable"
            )

    if failures:
        print("FAIL: notification email PHI gate")
        for f in failures:
            print(f"  - {f}")
        return 1

    print(
        f"PASS: {checked} email template(s) rendered PHI-free "
        f"against {len(PHI_TOKENS)} poisoned tokens"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
