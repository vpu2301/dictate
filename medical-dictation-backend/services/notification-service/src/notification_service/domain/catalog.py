"""The category catalogue — who hears about what, and how, by default.

ONE declarative table. Every routing decision the service makes is a
lookup here; nothing about recipients or default channels is scattered
across handlers.

`test_catalog.py` asserts this maps 1:1 onto `Category`, so adding an
enum member without an entry is a build failure rather than a category
that silently notifies nobody — the same "unknown intent is a bug"
contract as nlp-service's operations map.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from notification_events import Category, EmailMode, Severity


class RecipientRule(StrEnum):
    """How to resolve a fact's audience.

    The producer supplies `recipient_hints` for the rules it can answer
    (it knows a report's author). Role-derived audiences are resolved
    here against the membership table — a producer has no business
    querying who the tenant's admins are.
    """

    # The acting user's report: primary author + co-authors, minus the
    # actor (you don't need telling about your own action).
    REPORT_PARTICIPANTS = "report_participants"
    # Everyone with tenant_admin on the tenant. Used for integrity
    # failures, which are an operational concern, not a clinical one.
    TENANT_ADMINS = "tenant_admins"
    # Exactly the users the producer named, verbatim.
    EXPLICIT_HINTS = "explicit_hints"


@dataclass(frozen=True, slots=True)
class CategorySpec:
    category: Category
    recipient_rule: RecipientRule

    # Defaults, overridable per user (domain/preferences.py). A user who
    # has never expressed an opinion follows whatever these say today.
    default_in_app: bool
    default_email_mode: EmailMode

    severity: Severity

    # May a user route this to the daily digest instead of an immediate
    # email? Time-critical and failure categories say no — batching a
    # signing failure into tomorrow morning's summary is not a
    # preference, it is a defect.
    digest_eligible: bool

    # Whether the actor is excluded from their own fan-out.
    exclude_actor: bool = True

    # Template stem under `templates/email/`. Empty means this category
    # never emails, so it needs no template — asserted by the PHI gate,
    # which would otherwise report a missing file.
    email_template: str = ""


_SPECS: Final[tuple[CategorySpec, ...]] = (
    CategorySpec(
        category=Category.REPORT_FINALIZED,
        recipient_rule=RecipientRule.REPORT_PARTICIPANTS,
        default_in_app=True,
        default_email_mode=EmailMode.OFF,
        severity=Severity.INFO,
        digest_eligible=True,
        # The author DOES get told their own report finalized. The default
        # (exclude the actor) assumed finalize was something you do TO
        # someone else's report; in practice the overwhelmingly common
        # case is a solo clinician finalizing their own, and excluding
        # them meant that session produced no notification at all — the
        # feed stayed empty for exactly the user who was watching it.
        exclude_actor=False,
    ),
    CategorySpec(
        category=Category.REPORT_SIGNED,
        recipient_rule=RecipientRule.REPORT_PARTICIPANTS,
        default_in_app=True,
        default_email_mode=EmailMode.IMMEDIATE,
        severity=Severity.INFO,
        # A signature is a legal event; it goes out when it happens.
        digest_eligible=False,
        # The signer DOES get told their own signature committed — the
        # provider round-trip is asynchronous, so this is the completion
        # receipt for something they started and stopped watching.
        exclude_actor=False,
        email_template="report_signed",
    ),
    CategorySpec(
        category=Category.REPORT_SIGNING_FAILED,
        recipient_rule=RecipientRule.REPORT_PARTICIPANTS,
        default_in_app=True,
        default_email_mode=EmailMode.IMMEDIATE,
        severity=Severity.WARNING,
        digest_eligible=False,
        exclude_actor=False,
        email_template="report_signing_failed",
    ),
    CategorySpec(
        category=Category.REPORT_AMENDED,
        recipient_rule=RecipientRule.REPORT_PARTICIPANTS,
        default_in_app=True,
        default_email_mode=EmailMode.OFF,
        severity=Severity.INFO,
        digest_eligible=True,
    ),
    CategorySpec(
        category=Category.REPORT_CHAIN_FAILURE,
        recipient_rule=RecipientRule.TENANT_ADMINS,
        default_in_app=True,
        default_email_mode=EmailMode.IMMEDIATE,
        severity=Severity.CRITICAL,
        digest_eligible=False,
        # System-raised: there is no actor to exclude.
        exclude_actor=False,
        email_template="report_chain_failure",
    ),
    CategorySpec(
        category=Category.REPORT_SHARED_WITH_YOU,
        recipient_rule=RecipientRule.EXPLICIT_HINTS,
        default_in_app=True,
        default_email_mode=EmailMode.IMMEDIATE,
        severity=Severity.INFO,
        digest_eligible=True,
        email_template="report_shared",
    ),
    CategorySpec(
        category=Category.DICTATION_COMPLETED,
        # The dictating clinician, named by dictation-service, which owns
        # the session row. Nobody else has any interest in the fact that
        # a colleague stopped recording.
        recipient_rule=RecipientRule.EXPLICIT_HINTS,
        default_in_app=True,
        # Never emails. Finishing a dictation is a receipt you read in the
        # app you are already looking at; a mail per session would be the
        # single noisiest thing this service could do.
        default_email_mode=EmailMode.OFF,
        severity=Severity.INFO,
        digest_eligible=True,
        # The dictating user IS the audience. Excluding the actor here
        # would leave the category with no recipients at all.
        exclude_actor=False,
    ),
    CategorySpec(
        category=Category.TRANSCRIPTION_COMPLETED,
        # The user who submitted the job, named by asr-worker.
        recipient_rule=RecipientRule.EXPLICIT_HINTS,
        default_in_app=True,
        default_email_mode=EmailMode.OFF,
        severity=Severity.INFO,
        digest_eligible=True,
        # The submitter IS the audience — this is the receipt for a job
        # that ran for minutes while they looked away.
        exclude_actor=False,
    ),
    CategorySpec(
        category=Category.TRANSCRIPTION_FAILED,
        recipient_rule=RecipientRule.EXPLICIT_HINTS,
        default_in_app=True,
        # WARNING, not INFO: severity drives how long the SPA's toast
        # lingers, and a job that died needs to outlast a glance.
        # Silence here is worse than silence on success — the user waits
        # for a result that is never coming.
        severity=Severity.WARNING,
        # A failure the user must act on does not belong in tomorrow's
        # summary, matching report.signing_failed.
        digest_eligible=False,
        # No email, and therefore no template. The failure is only
        # actionable inside the app (resubmit the job), and every other
        # emailing category needed a PHI-gated template to say something
        # an in-app row already says.
        default_email_mode=EmailMode.OFF,
        exclude_actor=False,
    ),
    CategorySpec(
        category=Category.PHI_ACCESS_GRANTED,
        # The report's author and co-authors. They are the people who can
        # tell whether the stated reason holds up, and the ones whose
        # patient it is.
        recipient_rule=RecipientRule.REPORT_PARTICIPANTS,
        default_in_app=True,
        # WARNING, not INFO: an administrator reading a clinical record
        # is an exception, and the feed should present it as one.
        severity=Severity.WARNING,
        # No email, and therefore no template. The in-app row carries the
        # whole fact (who, which report, which reason), and a mail would
        # either repeat it or start reaching for detail that belongs in
        # the oversight view, not an inbox.
        default_email_mode=EmailMode.OFF,
        # Break-glass is the definition of a thing you do not batch into
        # tomorrow morning's summary.
        digest_eligible=False,
        # The actor is the ADMIN; the audience is the clinicians. The
        # default exclusion is exactly right here and left in place, so
        # an admin who also authors reports is not told about their own
        # break-glass.
    ),
    CategorySpec(
        category=Category.SECURITY_MFA_REMINDER,
        # Exactly the user who was asked, named by auth-service. This is
        # never a broadcast: telling a clinic that a colleague has no
        # second factor is publishing a weakness, not fixing one.
        recipient_rule=RecipientRule.EXPLICIT_HINTS,
        default_in_app=True,
        # WARNING: an account without a second factor is one stolen
        # password away from being someone else's, and the feed should
        # not present that with the same weight as "your report saved".
        severity=Severity.WARNING,
        # The one category that emails BECAUSE the recipient may not be
        # in the app — a user who has not enrolled is often a user who
        # signs in rarely, which is exactly who the in-app banner never
        # reaches.
        default_email_mode=EmailMode.IMMEDIATE,
        # A security ask does not wait for tomorrow's summary, and the
        # standing banner already covers the "later" case.
        digest_eligible=False,
        # The actor is the reviewer, the audience is the subject. They
        # cannot be the same person (the endpoint refuses self-reminders),
        # so the default exclusion would never fire — spelled out rather
        # than left to be re-derived.
        exclude_actor=False,
        email_template="security_mfa_reminder",
    ),
    CategorySpec(
        category=Category.SYSTEM_DIGEST,
        # The digest job addresses one user directly; it never fans out.
        recipient_rule=RecipientRule.EXPLICIT_HINTS,
        # The digest IS an email. An in-app copy of "here is a summary of
        # your in-app notifications" is noise.
        default_in_app=False,
        default_email_mode=EmailMode.IMMEDIATE,
        severity=Severity.INFO,
        digest_eligible=False,
        exclude_actor=False,
        email_template="digest",
    ),
)

CATALOG: Final[dict[Category, CategorySpec]] = {spec.category: spec for spec in _SPECS}


def spec_for(category: Category) -> CategorySpec:
    """Look up a category. Raises rather than guessing a default.

    An unknown category means producer and consumer disagree about the
    contract; inventing a routing decision would deliver the fact to the
    wrong audience, which is worse than failing the message into the DLQ
    where an operator can see it.
    """
    try:
        return CATALOG[category]
    except KeyError as exc:  # pragma: no cover — guarded by test_catalog
        raise KeyError(f"category {category!r} has no catalog entry") from exc


def digest_eligible_categories() -> frozenset[Category]:
    return frozenset(s.category for s in _SPECS if s.digest_eligible)


def emailing_categories() -> frozenset[Category]:
    """Categories that can ever produce an email — the PHI gate's input."""
    return frozenset(s.category for s in _SPECS if s.email_template)
