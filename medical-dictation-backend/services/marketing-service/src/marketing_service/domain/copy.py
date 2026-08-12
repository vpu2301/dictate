"""Subjects, and the dates the templates print.

The prose lives in the templates — they were designed and translated as
whole documents, and cutting them into string fragments here would make
the German copy impossible to proofread. What lives in this module is
only what the templates cannot hold: the subject line (a header, not
markup) and the handful of values that have to be *formatted* per
language rather than translated.

Dates are formatted by hand rather than through `locale`/`babel`.
`locale.setlocale` is process-global and not thread-safe, so a service
that renders Ukrainian on one request and German on the next would race
with itself; and pulling in babel for three languages and two formats is
a dependency for twelve lines of table.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Nominative month names for English, and GENITIVE for Ukrainian and
# German-with-a-day-number. "20 серпня" (of August), not "20 серпень" —
# the nominative reads as broken Ukrainian to a native speaker, and this
# mail is the first thing a prospect sees from us.
_MONTHS: Final[dict[str, tuple[str, ...]]] = {
    "en": (
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    ),
    "de": (
        "Januar", "Februar", "März", "April", "Mai", "Juni",
        "Juli", "August", "September", "Oktober", "November", "Dezember",
    ),
    "uk": (
        "січня", "лютого", "березня", "квітня", "травня", "червня",
        "липня", "серпня", "вересня", "жовтня", "листопада", "грудня",
    ),
}

_WEEKDAYS: Final[dict[str, tuple[str, ...]]] = {
    "en": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
    "de": (
        "Montag", "Dienstag", "Mittwoch", "Donnerstag",
        "Freitag", "Samstag", "Sonntag",
    ),
    "uk": (
        "понеділок", "вівторок", "середа", "четвер",
        "п'ятниця", "субота", "неділя",
    ),
}

SUBJECTS: Final[dict[str, dict[str, str]]] = {
    "request_received": {
        "en": "We have your request — pick a time for your Klarnote demo",
        "de": "Wir haben Ihre Anfrage — wählen Sie einen Termin für Ihre Klarnote-Demo",
        "uk": "Ми отримали ваш запит — оберіть час для демо Klarnote",
    },
    "demo_confirmed": {
        "en": "Your Klarnote demo is booked",
        "de": "Ihre Klarnote-Demo ist gebucht",
        "uk": "Ваше демо Klarnote заплановано",
    },
    # The contact form's acknowledgement. The subject promises the reply,
    # because that is what was asked for — the calendar is the alternative
    # offered inside, not the thing they came for, and a subject that led
    # with it would read as a form deflecting a question.
    # The subscription confirmation. States what was signed up for, because a
    # confirmation that only says "thank you" leaves the reader unable to tell
    # a newsletter from a sales sequence.
    "subscribe_confirmed": {
        "en": "You're subscribed — Klarnote",
        "de": "Ihr Abo ist bestätigt — Klarnote",
        "uk": "Ви підписані — Klarnote",
    },
    "contact_received": {
        "en": "Thank you for getting in touch — Klarnote",
        "de": "Danke für Ihre Nachricht — Klarnote",
        "uk": "Дякуємо за звернення — Klarnote",
    },
    # The internal forward. English only, by design (compose.contact_internal_
    # fields explains why), and the subject leads with the sender because that
    # is what a mailbox list view shows: "Contact form" alone is a row you have
    # to open to triage.
    "contact_internal": {
        "en": "Contact form — a new message is waiting",
    },
}

# What the greeting says when the form gave us no name. The demo form
# asks only for a work email, so this is the COMMON case, not the edge
# one — which is why the nameless variant is a proper greeting in each
# language rather than a fallback that reads like a mail-merge failure.
#
# The named Ukrainian variant does not decline the name into the
# vocative ("Олено" from "Олена") the designed template shows. Ukrainian
# vocative formation is not a rule you can apply blind — it depends on
# stem and gender, and getting it wrong on a first contact is worse than
# not attempting it. A nominative address is unremarkable; an invented
# one is not.
_GREETING_NAMELESS: Final[dict[str, str]] = {
    "en": "Hello,",
    "de": "Guten Tag,",
    "uk": "Доброго дня,",
}
_GREETING_NAMED: Final[dict[str, str]] = {
    "en": "Hello {name},",
    "de": "Guten Tag, {name},",
    "uk": "Доброго дня, {name},",
}


# The host's name in the reader's script. A Ukrainian letter signed
# "Andrii Kovalchuk" in Latin letters reads as a translation of a person,
# and the designed template signs it "Андрій Ковальчук" — so the
# transliteration is content, not configuration, and lives with the other
# per-language copy. Configuration supplies the Latin form (it is the one
# that changes when the seat changes hands); this maps it across.
_HOST_NAME_BY_LANG: Final[dict[str, dict[str, str]]] = {
    "Andrii Kovalchuk": {"uk": "Андрій Ковальчук"},
}

# Where the "request received" stamp is shown. It is a receipt, so it
# should read in the wall-clock the reader keeps — a Ukrainian doctor who
# submitted at 19:45 Kyiv must not be told we received it at 16:45.
# English falls to the company's own zone: it is the fallback language,
# read from anywhere, and there is nothing better to guess.
_DISPLAY_ZONE: Final[dict[str, str]] = {
    "uk": "Europe/Kyiv",
    "de": "Europe/Berlin",
    "en": "Europe/Kyiv",
}


def subject_for(kind: str, lang: str) -> str:
    by_lang = SUBJECTS[kind]
    return by_lang.get(lang, by_lang["en"])


def host_name_for(name: str, lang: str) -> str:
    return _HOST_NAME_BY_LANG.get(name, {}).get(lang, name)


def display_zone(lang: str) -> str:
    return _DISPLAY_ZONE.get(lang, _DISPLAY_ZONE["en"])


def greeting(name: str | None, lang: str) -> str:
    clean = (name or "").strip()
    if not clean:
        return _GREETING_NAMELESS.get(lang, _GREETING_NAMELESS["en"])
    template = _GREETING_NAMED.get(lang, _GREETING_NAMED["en"])
    return template.format(name=clean)


def long_date(moment: datetime, lang: str) -> str:
    """`Thursday, 20 August 2026` / `Donnerstag, 20. August 2026` / `четвер, 20 серпня 2026`."""
    weekday = _WEEKDAYS.get(lang, _WEEKDAYS["en"])[moment.weekday()]
    month = _MONTHS.get(lang, _MONTHS["en"])[moment.month - 1]
    if lang == "de":
        return f"{weekday}, {moment.day}. {month} {moment.year}"
    return f"{weekday}, {moment.day} {month} {moment.year}"


def short_datetime(moment: datetime, lang: str) -> str:
    """`9 August 2026, 19:45` — the "request received" stamp.

    Converted into the language's display zone first. The value stored on
    the row is UTC, and printing that verbatim tells someone who wrote to
    us at a quarter to eight that we received it at a quarter to five.
    """
    try:
        local = moment.astimezone(ZoneInfo(display_zone(lang)))
    except (ZoneInfoNotFoundError, ValueError):  # pragma: no cover — tzdata gap
        local = moment
    month = _MONTHS.get(lang, _MONTHS["en"])[local.month - 1]
    day = f"{local.day}. {month}" if lang == "de" else f"{local.day} {month}"
    return f"{day} {local.year}, {local:%H:%M}"


def zone_label(timezone_name: str, moment: datetime) -> str:
    """`Kyiv (UTC+3)`.

    The IANA name is unambiguous but reads as a machine identifier in a
    sales email; a bare abbreviation (EEST) is friendly but means
    nothing to most readers. City plus offset is understood by both, and
    unlike an abbreviation it cannot be misread across hemispheres.
    """
    city = (timezone_name.rsplit("/", 1)[-1] or "").replace("_", " ").strip()
    offset = moment.utcoffset()
    if offset is None:
        return city
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    stamp = f"UTC{sign}{hours}" + (f":{minutes:02d}" if minutes else "")
    return f"{city} ({stamp})" if city else stamp


def time_range(start: datetime, end: datetime, timezone_name: str) -> str:
    """`15:00 – 15:40 Kyiv (UTC+3)`."""
    label = zone_label(timezone_name, start)
    span = f"{start:%H:%M} – {end:%H:%M}"
    return f"{span} {label}".strip()


def duration_label(minutes: int, lang: str) -> str:
    if lang == "de":
        return f"{minutes} Minuten"
    if lang == "uk":
        return f"{minutes} хвилин"
    return f"{minutes} minutes"


# ── Plain-text alternates ────────────────────────────────────────────
#
# Not optional decoration. A multipart/alternative whose text part is
# missing (or is the HTML with the tags stripped) is a strong spam
# signal, and it is what a screen reader and a plain-text client
# actually get. These are shorter than the HTML on purpose: the text
# part's job is to carry the link and the facts, not to reproduce a
# designed layout in ASCII.
#
# `str.format` rather than Jinja: these are five-line strings with no
# logic, and routing them through the autoescaping HTML environment
# would escape the ampersands in the calendar URL into `&amp;`.
_TEXT: Final[dict[tuple[str, str], str]] = {
    # ── internal: the contact form's forward to the sales mailbox ────
    # `str.format` on a body that contains attacker-supplied text is safe
    # because the TEMPLATE holds the field names — a `{` typed into the web
    # form lands in a VALUE, and values are substituted, never re-scanned.
    #
    # The message is last and unindented, so the longest and most variable
    # part cannot push the facts below a preview pane's fold.
    ("contact_internal", "en"): """\
A new message came through the contact form on klarnote.com.

From:     {first_name} <{email}>
Company:  {organisation}
Reason:   {reason}
Language: {form_lang}
Received: {requested_at}
Page:     {source_page}

Reply to this email and the answer goes straight to the sender.

--- their message, verbatim ---

{message}

--- end of message ---""",
    # ── newsletter ──────────────────────────────────────────────────
    ("subscribe_confirmed", "en"): """\
{greeting}

You're subscribed.

Roughly once a month: what shipped, what we learned building a clinical
record system, and the occasional note on regulation that actually
affects a clinic. No drip sequence, and no sales follow-up unless you
ask for one.

Curious sooner? Book a demo and see it live:
{booking_url}

Your address: {email}
Subscribed: {requested_at}

One click unsubscribes, and it works from the first email onwards.

Talk soon,
{host_name}
Solutions Lead, Klarnote

You're receiving this because you subscribed at klarnote.com.
Unsubscribe: {unsubscribe_url}
Privacy: {privacy_url}""",
    ("subscribe_confirmed", "de"): """\
{greeting}

Ihr Abo ist bestätigt.

Etwa einmal im Monat: was neu ist, was wir beim Bau eines klinischen
Dokumentationssystems gelernt haben, und gelegentlich eine Notiz zu
Regulierung, die eine Praxis wirklich betrifft. Keine Drip-Kampagne und
kein Vertriebs-Nachfassen, sofern Sie nicht darum bitten.

Neugierig auf mehr? Buchen Sie eine Demo und sehen Sie es live:
{booking_url}

Ihre Adresse: {email}
Angemeldet: {requested_at}

Ein Klick genügt zum Abmelden — schon ab der ersten E-Mail.

Bis bald,
{host_name}
Solutions Lead, Klarnote

Sie erhalten diese E-Mail, weil Sie sich auf klarnote.com angemeldet haben.
Abmelden: {unsubscribe_url}
Datenschutz: {privacy_url}""",
    ("subscribe_confirmed", "uk"): """\
{greeting}

ви підписані.

Приблизно раз на місяць: що вийшло нового, чого ми навчилися, будуючи
систему медичної документації, і час від часу — про регулювання, яке
справді стосується клініки. Жодних автоматичних серій і жодних
дзвінків від продажів, якщо ви самі не попросите.

Цікаво раніше? Забронюйте демо й побачте все живцем:
{booking_url}

Ваша адреса: {email}
Підписано: {requested_at}

Відписатися — один клік, і він працює з першого ж листа.

До зв'язку,
{host_name}
Solutions Lead, Klarnote

Ви отримали цей лист, бо підписалися на klarnote.com.
Відписатися: {unsubscribe_url}
Приватність: {privacy_url}""",
    # ── contact form ────────────────────────────────────────────────
    # Same variables as request_received, so the two share a compose call.
    # The order of the two facts is the whole difference: a person is
    # answering you, AND the calendar is there if you would rather not wait.
    ("contact_received", "en"): """{greeting}

Thank you for getting in touch — your message has reached us.

A manager will read it and reply personally to {email}. We answer within
one business day.

If you would rather not wait, you can book a demo straight away and pick
a time that suits you. It runs {duration_label} on Google Meet — a live
walk through the product and answers to whatever you bring.

Book a demo:
{booking_url}

Message received: {requested_at}

Talk soon,
{host_name}
Solutions Lead, Klarnote

You're receiving this because you wrote to us at klarnote.com.
Unsubscribe: {unsubscribe_url}
Privacy: {privacy_url}""",
    ("contact_received", "de"): """{greeting}

danke für Ihre Nachricht — sie ist bei uns angekommen.

Ein Mitarbeiter liest sie und antwortet Ihnen persönlich an {email}. Wir
melden uns innerhalb eines Werktages.

Wenn Sie nicht warten möchten, können Sie direkt eine Demo buchen und
einen Termin wählen, der Ihnen passt. Sie dauert {duration_label} und
findet per Google Meet statt — live durch das Produkt, mit Antworten auf
Ihre Fragen.

Demo buchen:
{booking_url}

Nachricht eingegangen: {requested_at}

Bis bald,
{host_name}
Solutions Lead, Klarnote

Sie erhalten diese E-Mail, weil Sie uns über klarnote.com geschrieben haben.
Abmelden: {unsubscribe_url}
Datenschutz: {privacy_url}""",
    ("contact_received", "uk"): """{greeting}

дякуємо за звернення — ваше повідомлення в нас.

Менеджер прочитає його й особисто відповість на {email}. Ми відповідаємо
протягом одного робочого дня.

Якщо не хочете чекати, можете одразу забронювати демо й обрати зручний
час. Воно триває {duration_label} у Google Meet — живий огляд продукту та
відповіді на ваші питання.

Забронювати демо:
{booking_url}

Повідомлення отримано: {requested_at}

До зв'язку,
{host_name}
Solutions Lead, Klarnote

Ви отримали цей лист, бо написали нам на klarnote.com.
Відписатися: {unsubscribe_url}
Приватність: {privacy_url}""",
    ("request_received", "en"): """\
{greeting}

Thank you — we have your request.

The next step is yours: pick a time that suits you. The demo runs
{duration_label} on Google Meet — a live walk through the product and
answers to whatever you bring.

Pick a time for the demo:
{booking_url}

Your address: {email}
Request received: {requested_at}

If another way suits you better, just reply to this email — {host_name}
reads it personally and will suggest a time, including outside the
standard slots.

Talk soon,
{host_name}
Solutions Lead, Klarnote

You're receiving this because you requested a demo at klarnote.com.
Unsubscribe: {unsubscribe_url}
Privacy: {privacy_url}""",
    ("request_received", "de"): """\
{greeting}

danke — Ihre Anfrage ist da.

Der nächste Schritt liegt bei Ihnen: Wählen Sie einen Termin, der Ihnen
passt. Die Demo dauert {duration_label} und findet per Google Meet statt —
live durch das Produkt, mit Antworten auf Ihre Fragen.

Termin für die Demo wählen:
{booking_url}

Ihre Adresse: {email}
Anfrage eingegangen: {requested_at}

Wenn Ihnen ein anderer Weg lieber ist, antworten Sie einfach auf diese
E-Mail — {host_name} liest persönlich mit und schlägt Ihnen einen Termin
vor, auch außerhalb der üblichen Zeiten.

Bis bald,
{host_name}
Solutions Lead, Klarnote

Sie erhalten diese E-Mail, weil Sie auf klarnote.com eine Demo angefragt haben.
Abmelden: {unsubscribe_url}
Datenschutz: {privacy_url}""",
    ("request_received", "uk"): """\
{greeting}

дякуємо — ми отримали ваш запит.

Наступний крок за вами: оберіть час, коли вам зручно. Демо триває
{duration_label} і проходить у Google Meet — покажемо продукт живцем
і відповімо на запитання.

Обрати час для демо:
{booking_url}

Ваша адреса: {email}
Запит отримано: {requested_at}

Якщо вам зручніше інакше — просто відповідайте на цей лист. {host_name}
прочитає особисто й запропонує час, зокрема поза стандартними слотами.

До зв'язку,
{host_name}
Solutions Lead, Klarnote

Ви отримали цей лист, бо залишили запит на демо на klarnote.com.
Відписатися: {unsubscribe_url}
Конфіденційність: {privacy_url}""",
    ("demo_confirmed", "en"): """\
{greeting}

Your demo is booked.

Date:   {date_label}
Time:   {time_label}
Length: {duration_label}
Where:  Google Meet
Host:   {host_name} · Solutions Lead

Join on Google Meet:
{meet_url}

Add to Google Calendar:
{add_to_calendar_url}

One small favour: reply with one or two typical cases, or the report
template you use today — we'll demo your workflow rather than a
generic tour.

Need to change the time? {booking_url}

See you then,
{host_name}
Solutions Lead, Klarnote

You're receiving this because you requested a demo at klarnote.com.
Unsubscribe: {unsubscribe_url}
Privacy: {privacy_url}""",
    ("demo_confirmed", "de"): """\
{greeting}

Ihre Demo ist gebucht.

Datum:  {date_label}
Zeit:   {time_label}
Dauer:  {duration_label}
Wo:     Google Meet
Gastgeber: {host_name} · Solutions Lead

Per Google Meet beitreten:
{meet_url}

Zum Google Kalender hinzufügen:
{add_to_calendar_url}

Eine kleine Bitte: Antworten Sie mit ein oder zwei typischen Fällen oder
der Berichtsvorlage, die Sie heute nutzen — dann zeigen wir Ihren
Arbeitsablauf statt einer allgemeinen Tour.

Termin ändern? {booking_url}

Bis dann,
{host_name}
Solutions Lead, Klarnote

Sie erhalten diese E-Mail, weil Sie auf klarnote.com eine Demo angefragt haben.
Abmelden: {unsubscribe_url}
Datenschutz: {privacy_url}""",
    ("demo_confirmed", "uk"): """\
{greeting}

ваше демо заброньовано.

Дата:      {date_label}
Час:       {time_label}
Тривалість: {duration_label}
Де:        Google Meet
Ведучий:   {host_name} · Solutions Lead

Приєднатися в Google Meet:
{meet_url}

Додати в Google Календар:
{add_to_calendar_url}

Маленьке прохання: надішліть у відповідь один-два типові випадки або
шаблон звіту, яким користуєтесь сьогодні — покажемо саме ваш робочий
процес, а не загальну екскурсію.

Потрібно змінити час? {booking_url}

До зустрічі,
{host_name}
Solutions Lead, Klarnote

Ви отримали цей лист, бо залишили запит на демо на klarnote.com.
Відписатися: {unsubscribe_url}
Конфіденційність: {privacy_url}""",
}


def text_body(kind: str, lang: str, values: dict[str, str]) -> str:
    """Render the plain-text alternate.

    Missing keys raise KeyError rather than rendering `{meet_url}`
    literally into a prospect's inbox — same reasoning as
    StrictUndefined on the HTML side.
    """
    template = _TEXT.get((kind, lang)) or _TEXT[(kind, "en")]
    return template.format(**values)
