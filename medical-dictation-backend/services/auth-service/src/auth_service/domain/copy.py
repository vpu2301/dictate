"""Subjects, greetings, dates and plain-text bodies for account mail.

Same split marketing-service uses: the HTML prose lives in whole,
per-language template files so a proofreader who does not read Python can
check the German and Ukrainian; everything that has to be *computed* —
subject lines, a greeting that changes with whether we know a name, a
formatted timestamp — lives here.

The plain-text alternates use ``str.format`` rather than Jinja, and that
is not an oversight: Jinja's autoescaping would turn the ``&`` in a URL
query string into ``&amp;`` inside a text/plain part, where it is not
markup and the link would arrive broken.

Dates are formatted by hand rather than through ``locale``: the C locale
is process-global and not thread-safe, so one request formatting a
Ukrainian date would change what every concurrent request produced.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final
from zoneinfo import ZoneInfo

SUPPORTED_LANGS: Final[tuple[str, ...]] = ("en", "de", "uk")
DEFAULT_LANG: Final = "en"

KIND_PASSWORD_RESET: Final = "password_reset"
KIND_PASSWORD_CHANGED: Final = "password_changed"
KINDS: Final[tuple[str, ...]] = (KIND_PASSWORD_RESET, KIND_PASSWORD_CHANGED)


def normalise_lang(lang: str | None) -> str:
    """Coerce anything to a language we actually have templates for."""
    if not lang:
        return DEFAULT_LANG
    base = lang.strip().lower().split("-", 1)[0]
    return base if base in SUPPORTED_LANGS else DEFAULT_LANG


# ── Subjects ─────────────────────────────────────────────────────────
#
# The security-notification subject deliberately leads with the fact, not
# a question. "Was this you?" in a subject line is the exact shape of a
# phishing lure, and training users to click it is the opposite of what
# this mail is for.

SUBJECTS: Final[dict[str, dict[str, str]]] = {
    KIND_PASSWORD_RESET: {
        "en": "Reset your Klarnote password",
        "de": "Setzen Sie Ihr Klarnote-Passwort zurück",
        "uk": "Відновлення пароля Klarnote",
    },
    KIND_PASSWORD_CHANGED: {
        "en": "Your Klarnote password was changed",
        "de": "Ihr Klarnote-Passwort wurde geändert",
        "uk": "Пароль Klarnote було змінено",
    },
}


def subject_for(kind: str, lang: str) -> str:
    by_lang = SUBJECTS[kind]
    return by_lang.get(lang, by_lang[DEFAULT_LANG])


# ── Greetings ────────────────────────────────────────────────────────

_GREETING_NAMED: Final[dict[str, str]] = {
    "en": "Hello {name},",
    "de": "Hallo {name},",
    "uk": "Вітаємо, {name},",
}
_GREETING_NAMELESS: Final[dict[str, str]] = {
    "en": "Hello,",
    "de": "Hallo,",
    "uk": "Вітаємо,",
}


def greeting(lang: str, display_name: str = "") -> str:
    name = (display_name or "").strip()
    if not name:
        return _GREETING_NAMELESS.get(lang, _GREETING_NAMELESS[DEFAULT_LANG])
    template = _GREETING_NAMED.get(lang, _GREETING_NAMED[DEFAULT_LANG])
    return template.format(name=name)


# ── Timestamps ───────────────────────────────────────────────────────

_MONTHS: Final[dict[str, tuple[str, ...]]] = {
    "en": (
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    ),
    "de": (
        "Januar", "Februar", "März", "April", "Mai", "Juni",
        "Juli", "August", "September", "Oktober", "November", "Dezember",
    ),
    # Genitive — Ukrainian dates read "5 серпня", not "5 серпень".
    "uk": (
        "січня", "лютого", "березня", "квітня", "травня", "червня",
        "липня", "серпня", "вересня", "жовтня", "листопада", "грудня",
    ),
}

_DISPLAY_ZONE: Final[dict[str, str]] = {
    "en": "Europe/Kyiv",
    "uk": "Europe/Kyiv",
    "de": "Europe/Berlin",
}


def format_moment(when: datetime, lang: str) -> str:
    """A timestamp a human can check against their own memory.

    Shown in the recipient's likely local zone with the zone named, since
    "was that me at 03:14?" is the entire question the security mail
    asks, and an answer in UTC makes it harder to answer, not easier.
    """
    zone = ZoneInfo(_DISPLAY_ZONE.get(lang, "Europe/Kyiv"))
    local = when.astimezone(zone)
    month = _MONTHS.get(lang, _MONTHS[DEFAULT_LANG])[local.month - 1]
    abbrev = local.tzname() or ""
    if lang == "uk":
        return f"{local.day} {month} {local.year}, {local:%H:%M} ({abbrev})"
    if lang == "de":
        return f"{local.day}. {month} {local.year}, {local:%H:%M} ({abbrev})"
    return f"{month} {local.day}, {local.year}, {local:%H:%M} ({abbrev})"


def minutes_label(seconds: int, lang: str) -> str:
    """"30 minutes" / "30 Minuten" / "30 хвилин", pluralised properly."""
    minutes = max(1, round(seconds / 60))
    if lang == "de":
        return f"{minutes} Minute" if minutes == 1 else f"{minutes} Minuten"
    if lang == "uk":
        # Ukrainian needs three forms; the teens are the trap that a
        # simple `n == 1` check gets wrong (11 takes the plural).
        tail_two = minutes % 100
        tail_one = minutes % 10
        if 11 <= tail_two <= 14:
            word = "хвилин"
        elif tail_one == 1:
            word = "хвилина"
        elif 2 <= tail_one <= 4:
            word = "хвилини"
        else:
            word = "хвилин"
        return f"{minutes} {word}"
    return f"{minutes} minute" if minutes == 1 else f"{minutes} minutes"


# ── Plain-text alternates ────────────────────────────────────────────
#
# Every mail ships both parts. A text/plain alternate is what a screen
# reader, a text-mode client, and most spam filters actually read, and a
# mail without one scores measurably worse on delivery.

_TEXT: Final[dict[tuple[str, str], str]] = {
    (KIND_PASSWORD_RESET, "en"): """\
{greeting}

Someone asked to reset the password for your Klarnote account ({email}).
If that was you, open the link below to choose a new one:

{reset_url}

The link works once and expires in {expiry_label}.

If you did not ask for this, you can ignore this email — your password
has not changed and nobody can see it. Nothing happens until the link
above is opened.

Requested: {requested_at}
{client_line}
Need help? {support_url}

— Klarnote
""",
    (KIND_PASSWORD_RESET, "de"): """\
{greeting}

Jemand hat angefordert, das Passwort für Ihr Klarnote-Konto ({email})
zurückzusetzen. Falls Sie das waren, wählen Sie über den folgenden Link
ein neues Passwort:

{reset_url}

Der Link funktioniert einmal und läuft in {expiry_label} ab.

Falls Sie das nicht angefordert haben, können Sie diese E-Mail
ignorieren — Ihr Passwort wurde nicht geändert und niemand kann es
einsehen. Es passiert nichts, solange der Link nicht geöffnet wird.

Angefordert: {requested_at}
{client_line}
Brauchen Sie Hilfe? {support_url}

— Klarnote
""",
    (KIND_PASSWORD_RESET, "uk"): """\
{greeting}

Надійшов запит на відновлення пароля до вашого облікового запису
Klarnote ({email}). Якщо це були ви, перейдіть за посиланням нижче, щоб
обрати новий пароль:

{reset_url}

Посилання діє один раз і втрачає чинність через {expiry_label}.

Якщо ви цього не робили — просто проігноруйте цей лист. Ваш пароль не
змінено, і ніхто не може його побачити. Нічого не станеться, доки
посилання вище не буде відкрито.

Запит надіслано: {requested_at}
{client_line}
Потрібна допомога? {support_url}

— Klarnote
""",
    (KIND_PASSWORD_CHANGED, "en"): """\
{greeting}

The password for your Klarnote account ({email}) was just changed.

Changed: {changed_at}
{client_line}
IF THIS WAS YOU, there is nothing to do.

IF THIS WAS NOT YOU, act now — someone else may control your account.
Open this link to sign out every device immediately and start a fresh
password reset:

{lockdown_url}

That link ends every active session, cancels any pending reset link, and
lets you set a new password yourself. It stays valid for {lockdown_expiry_label}.

Need help? {support_url}

— Klarnote
""",
    (KIND_PASSWORD_CHANGED, "de"): """\
{greeting}

Das Passwort für Ihr Klarnote-Konto ({email}) wurde soeben geändert.

Geändert: {changed_at}
{client_line}
WAREN SIE DAS, müssen Sie nichts tun.

WAREN SIE DAS NICHT, handeln Sie jetzt — möglicherweise hat jemand
anderes Zugriff auf Ihr Konto. Öffnen Sie diesen Link, um sofort alle
Geräte abzumelden und eine neue Passwortzurücksetzung zu starten:

{lockdown_url}

Der Link beendet alle aktiven Sitzungen, verwirft ausstehende
Zurücksetzungs-Links und lässt Sie selbst ein neues Passwort setzen. Er
bleibt {lockdown_expiry_label} gültig.

Brauchen Sie Hilfe? {support_url}

— Klarnote
""",
    (KIND_PASSWORD_CHANGED, "uk"): """\
{greeting}

Пароль до вашого облікового запису Klarnote ({email}) щойно змінено.

Змінено: {changed_at}
{client_line}
ЯКЩО ЦЕ БУЛИ ВИ — робити нічого не потрібно.

ЯКЩО ЦЕ БУЛИ НЕ ВИ — дійте негайно, доступ до вашого облікового запису
може мати стороння особа. Відкрийте це посилання, щоб миттєво завершити
сеанси на всіх пристроях і розпочати нове відновлення пароля:

{lockdown_url}

Посилання завершує всі активні сеанси, скасовує невикористані посилання
для відновлення та дає змогу самостійно встановити новий пароль. Воно
дійсне протягом {lockdown_expiry_label}.

Потрібна допомога? {support_url}

— Klarnote
""",
}

_CLIENT_LINE: Final[dict[str, str]] = {
    "en": "Where from: {client_label}\n",
    "de": "Woher: {client_label}\n",
    "uk": "Звідки: {client_label}\n",
}


def client_line(lang: str, client_label: str) -> str:
    """One line describing the requesting client, or nothing.

    Empty when we have no usable description — an empty "Where from:"
    label reads like missing data and invites the reader to distrust the
    rest of the mail.
    """
    label = (client_label or "").strip()
    if not label:
        return ""
    return _CLIENT_LINE.get(lang, _CLIENT_LINE[DEFAULT_LANG]).format(
        client_label=label
    )


def text_body(kind: str, lang: str, values: dict[str, str]) -> str:
    """Render the plain-text alternate. Raises ``KeyError`` on a gap.

    Deliberately strict: a missing variable that rendered as an empty
    string would produce a mail telling somebody to open a blank link.
    """
    template = _TEXT.get((kind, lang)) or _TEXT[(kind, DEFAULT_LANG)]
    return template.format(**values)


def utcnow() -> datetime:
    return datetime.now(UTC)
