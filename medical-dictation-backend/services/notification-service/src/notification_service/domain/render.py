"""Renders a fact into the PHI-free text a user actually sees.

The PHI boundary is enforced by ALLOW-LISTING payload keys per category
rather than by scrubbing what a producer sent. Scrubbing is a losing
game — it can only remove the patterns someone thought of, and a
Ukrainian surname is not a pattern. An allow-list inverts the burden: a
producer that adds `patient_name` to a payload finds it silently unused
here, and adding it to a template requires an explicit edit that shows
up in the diff a DPO reviews (ADR-0031).

Every string that reaches this module is additionally clamped, so even
an allow-listed field cannot become an exfiltration channel by being
very long.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final
from uuid import UUID

from notification_events import Category, NotificationEvent

from .catalog import spec_for

# The ONLY payload keys any template may read, per category. A key not
# listed here is invisible to rendering no matter what a producer sends.
ALLOWED_PAYLOAD_KEYS: Final[dict[Category, frozenset[str]]] = {
    Category.REPORT_FINALIZED: frozenset({"report_code"}),
    Category.REPORT_SIGNED: frozenset({"report_code", "signature_level"}),
    Category.REPORT_SIGNING_FAILED: frozenset({"report_code", "failure_reason", "provider"}),
    Category.REPORT_AMENDED: frozenset({"report_code", "version"}),
    Category.REPORT_CHAIN_FAILURE: frozenset({"report_code", "detected_at", "check_name"}),
    Category.REPORT_SHARED_WITH_YOU: frozenset({"report_code", "shared_by_display"}),
    # Counts and durations only. The transcript is the PHI here, and no
    # amount of it — not even a leading fragment as a "preview" — is
    # admissible: this row is read back by the digest renderer too.
    Category.DICTATION_COMPLETED: frozenset({"duration_ms", "segments"}),
    # Same counts-only rule as a dictation. Deliberately NOT the audio
    # filename: a clinician naming an upload `ivanenko_2026-04-12.wav`
    # would put a surname and a DOB in a notification title.
    Category.TRANSCRIPTION_COMPLETED: frozenset(
        {"duration_ms", "segments", "language", "model"}
    ),
    # `error_kind` is a closed vocabulary (corrupt_audio / timeout /
    # gpu_oom). `error_detail` is NOT admitted: it is free text built
    # from an exception, and an exception that quotes the transcript it
    # choked on would carry PHI straight into the feed.
    Category.TRANSCRIPTION_FAILED: frozenset({"error_kind"}),
    # `reason_code` is a closed vocabulary (see the CHECK on
    # phi_access_requests). `reason_note` is NOT admitted: it is free
    # text an administrator typed about a specific patient situation,
    # and it belongs in the oversight view behind an authz check, not in
    # a notification row that the digest renderer reads back.
    # `requested_by_display` is a staff name, never a patient's.
    Category.PHI_ACCESS_GRANTED: frozenset(
        {"report_code", "reason_code", "requested_by_display", "expires_at"}
    ),
    # No patient ever appears in this one — it is about the recipient's own
    # account. `requested_by_role` is the closed vocabulary from the
    # mfa_reminders CHECK (tenant_admin | auditor), not a name: who filed
    # an access-review finding is between the reviewer and the audit log,
    # and naming them turns a security ask into an interpersonal one.
    # `reminder_count` is admitted so a second ask reads as a second ask.
    Category.SECURITY_MFA_REMINDER: frozenset({"requested_by_role", "reminder_count"}),
    Category.SYSTEM_DIGEST: frozenset({"count", "period"}),
}

# Ukrainian labels for the reminder's requester vocabulary.
_REMINDER_ROLE_LABELS_UK: Final[dict[str, str]] = {
    "tenant_admin": "Адміністратор клініки",
    "auditor": "Аудитор",
}

# Ukrainian labels for the break-glass reason vocabulary. A raw
# `billing_dispute` in a clinician's feed is not a notification, it is a
# database value that leaked into a UI.
_REASON_LABELS_UK: Final[dict[str, str]] = {
    "patient_complaint": "скарга пацієнта",
    "legal_request": "юридичний запит",
    "billing_dispute": "спір щодо оплати",
    "quality_review": "перевірка якості",
    "care_continuity": "безперервність надання допомоги",
    "data_correction": "виправлення даних",
    "other": "інша причина",
}

# Field clamp. Long enough for a report code or a provider name, far too
# short to carry a clinical narrative.
MAX_FIELD_LEN: Final = 120


def safe_payload(event: NotificationEvent) -> dict[str, str]:
    """Project a payload down to its allow-listed, clamped, stringified keys."""
    allowed = ALLOWED_PAYLOAD_KEYS.get(event.category, frozenset())
    out: dict[str, str] = {}
    for key in allowed:
        if key not in event.payload:
            continue
        value = event.payload[key]
        if value is None:
            continue
        out[key] = str(value)[:MAX_FIELD_LEN]
    return out


def _code(fields: Mapping[str, str]) -> str:
    """The report's human-facing code — a pointer, never a title.

    Report titles are NOT used: a clinician-authored title routinely
    contains a diagnosis, which would put PHI in an email subject line.
    """
    return fields.get("report_code", "—")


def deep_link(event: NotificationEvent, *, base_url: str) -> str:
    """A path into the SPA. Carries ids, never content."""
    return notification_deep_link(
        event.resource_type, event.resource_id, base_url=base_url
    )


def render_title(event: NotificationEvent) -> str:
    fields = safe_payload(event)
    code = _code(fields)
    match event.category:
        case Category.REPORT_FINALIZED:
            return f"Звіт {code} завершено"
        case Category.REPORT_SIGNED:
            return f"Звіт {code} підписано"
        case Category.REPORT_SIGNING_FAILED:
            return f"Не вдалося підписати звіт {code}"
        case Category.REPORT_AMENDED:
            return f"Звіт {code} доповнено"
        case Category.REPORT_CHAIN_FAILURE:
            return f"Порушення цілісності журналу ({code})"
        case Category.REPORT_SHARED_WITH_YOU:
            return f"Вам надано доступ до звіту {code}"
        case Category.DICTATION_COMPLETED:
            return "Диктування завершено"
        case Category.TRANSCRIPTION_COMPLETED:
            return "Розшифровку аудіо завершено"
        case Category.TRANSCRIPTION_FAILED:
            return "Не вдалося розшифрувати аудіо"
        case Category.PHI_ACCESS_GRANTED:
            return f"Адміністратор відкрив звіт {code}"
        case Category.SECURITY_MFA_REMINDER:
            return "Увімкніть двофакторну автентифікацію"
        case Category.SYSTEM_DIGEST:
            return f"Ваші сповіщення: {fields.get('count', '0')}"
    # Unreachable: spec_for() has already rejected unknown categories.
    raise KeyError(event.category)


def render_body(event: NotificationEvent) -> str:
    fields = safe_payload(event)
    code = _code(fields)
    match event.category:
        case Category.REPORT_FINALIZED:
            return f"Звіт {code} переведено у статус «завершено»."
        case Category.REPORT_SIGNED:
            level = fields.get("signature_level", "")
            suffix = f" Рівень підпису: {level}." if level else ""
            return f"Кваліфікований електронний підпис для звіту {code} накладено.{suffix}"
        case Category.REPORT_SIGNING_FAILED:
            reason = fields.get("failure_reason", "невідома причина")
            return f"Сесію підписання звіту {code} не завершено: {reason}."
        case Category.REPORT_AMENDED:
            version = fields.get("version", "")
            suffix = f" Версія {version}." if version else ""
            return f"До звіту {code} додано доповнення.{suffix}"
        case Category.REPORT_CHAIN_FAILURE:
            check = fields.get("check_name", "перевірка цілісності")
            return (
                f"Автоматична перевірка «{check}» виявила розбіжність "
                "у ланцюжку версій. Потрібна дія адміністратора."
            )
        case Category.REPORT_SHARED_WITH_YOU:
            who = fields.get("shared_by_display", "Колега")
            return f"{who} надав(-ла) вам доступ до звіту {code}."
        case Category.DICTATION_COMPLETED:
            return f"Сеанс диктування оброблено. Сегментів: {fields.get('segments', '0')}."
        case Category.TRANSCRIPTION_COMPLETED:
            return (
                f"Аудіозапис розшифровано. Сегментів: {fields.get('segments', '0')}. "
                "Можна створити звіт."
            )
        case Category.TRANSCRIPTION_FAILED:
            kind = fields.get("error_kind", "невідома причина")
            return f"Завдання на розшифровку не виконано: {kind}. Спробуйте ще раз."
        case Category.PHI_ACCESS_GRANTED:
            who = fields.get("requested_by_display", "Адміністратор")
            raw_reason = fields.get("reason_code", "")
            reason = _REASON_LABELS_UK.get(raw_reason, raw_reason or "причину не вказано")
            until = fields.get("expires_at", "")
            suffix = f" Доступ діє до {until}." if until else ""
            return (
                f"{who} отримав(-ла) тимчасовий доступ до звіту {code}. "
                f"Підстава: {reason}.{suffix}"
            )
        case Category.SECURITY_MFA_REMINDER:
            raw_role = fields.get("requested_by_role", "")
            who = _REMINDER_ROLE_LABELS_UK.get(raw_role, "Служба безпеки клініки")
            try:
                again = int(fields.get("reminder_count", "1")) > 1
            except (TypeError, ValueError):
                again = False
            prefix = f"{who} повторно просить" if again else f"{who} просить"
            return (
                f"{prefix} вас увімкнути двофакторну автентифікацію (TOTP) "
                "для вашого облікового запису. Це займає близько хвилини."
            )
        case Category.SYSTEM_DIGEST:
            return f"Підсумок за {fields.get('period', 'день')}."
    raise KeyError(event.category)


def severity_for(event: NotificationEvent) -> str:
    return str(spec_for(event.category).severity)


def coalesced_title(category: Category, count: int) -> str:
    """Title for a storm-coalesced row (E1)."""
    match category:
        case Category.REPORT_FINALIZED:
            return f"Завершено звітів: {count}"
        case Category.REPORT_AMENDED:
            return f"Доповнено звітів: {count}"
        case Category.DICTATION_COMPLETED:
            return f"Завершено сеансів диктування: {count}"
        case Category.TRANSCRIPTION_COMPLETED:
            return f"Розшифровано аудіозаписів: {count}"
        case _:
            return f"Нових сповіщень: {count}"


def coalesced_body(count: int) -> str:
    return (
        f"Згруповано {count} однотипних сповіщень, щоб не переповнювати стрічку. "
        "Відкрийте список, щоб переглянути кожне."
    )


def notification_deep_link(resource_type: str, resource_id: UUID, *, base_url: str) -> str:
    base = base_url.rstrip("/")
    if resource_type == "report":
        return f"{base}/reports/{resource_id}"
    if resource_type == "dictation_session":
        return f"{base}/dictations/{resource_id}"
    if resource_type == "transcription_job":
        return f"{base}/asr/jobs/{resource_id}"
    # The MFA reminder's resource is the USER — and the only useful place to
    # send them is enrolment, not a profile page. `{sub}` is deliberately not
    # in the path: the SPA enrols whoever is signed in, and a link carrying
    # somebody's id would be a link that looks actionable by anyone.
    if resource_type == "user":
        return f"{base}/mfa"
    return base
