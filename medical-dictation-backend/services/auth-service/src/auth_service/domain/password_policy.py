"""How we decide a chosen password is good enough.

Follows NIST SP 800-63B §5.1.1.2 rather than the composition rules most
people expect, and the difference is deliberate:

  * **Length is the control.** 12 characters minimum, and a generous
    upper bound (128) so passphrases and password-manager output are not
    truncated. No "must contain an uppercase and a symbol" rule —
    800-63B §5.1.1.2 explicitly says verifiers SHOULD NOT impose them,
    because they push users towards `Passw0rd!` and nothing else.
  * **Blocklist instead.** The rule that does earn its place is refusing
    passwords already known to attackers, plus anything derived from the
    user's own identifiers. That is what actually fails in a credential
    stuffing attack.
  * **No expiry, no reuse history.** 800-63B §5.1.1.2 recommends against
    forced rotation, and keeping prior password hashes would mean this
    system storing more verifiers than Keycloak already does.

The blocklist here is small and self-contained on purpose. A real
deployment should point ``MDX_PASSWORD_BLOCKLIST_PATH`` at a Pwned
Passwords k-anonymity range file or run the k-anonymity API; that is a
deployment concern with an ongoing data dependency, and shipping a
30-million-line file in the image is not the answer. What is in the
module is the top of the list every automated attack starts with, plus
the structural checks (identifier reuse, single repeated character,
sequential runs) that a blocklist cannot express.

Returned as a list of machine-readable codes rather than prose: the SPA
renders them in the user's language, and the strings live there.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Final

MIN_LENGTH_FLOOR: Final = 8
MAX_LENGTH: Final = 128

# Reason codes. Mirrored by the SPA's message table — a code added here
# without a matching entry there renders as a generic failure.
TOO_SHORT: Final = "too_short"
TOO_LONG: Final = "too_long"
COMMON: Final = "common"
CONTAINS_IDENTIFIER: Final = "contains_identifier"
REPEATED: Final = "repeated"
SEQUENTIAL: Final = "sequential"
WHITESPACE_ONLY: Final = "whitespace_only"

# The head of every credential-stuffing wordlist. Stored casefolded and
# with digits intact; the check also strips trivial leet substitutions so
# `P@ssw0rd` does not sail past a list containing `password`.
_COMMON_PASSWORDS: Final[frozenset[str]] = frozenset(
    {
        "123456", "123456789", "12345678", "1234567890", "1234567",
        "password", "password1", "password123", "passwort", "пароль",
        "qwerty", "qwerty123", "qwertyuiop", "asdfghjkl", "zxcvbnm",
        "111111", "000000", "123123", "654321", "666666", "121212",
        "iloveyou", "admin", "administrator", "welcome", "welcome1",
        "monkey", "dragon", "sunshine", "princess", "football",
        "letmein", "abc123", "trustno1", "master", "shadow", "superman",
        "michael", "jennifer", "jordan", "hunter", "harley", "ranger",
        "changeme", "secret", "default", "root", "toor", "test", "test123",
        "klarnote", "medicaldictation", "dictation", "clinic", "hospital",
        "doctor", "medical", "patient", "qwerty12345", "1q2w3e4r",
        "1qaz2wsx", "zaq12wsx", "q1w2e3r4t5", "passw0rd", "p@ssw0rd",
        "dev-password", "devpassword", "changeit", "temporary",
    }
)

_LEET: Final[dict[str, str]] = {
    "0": "o", "1": "l", "3": "e", "4": "a", "5": "s", "7": "t",
    "@": "a", "$": "s", "!": "i", "|": "l",
}

# Runs long enough to be a pattern rather than a coincidence.
_SEQUENCES: Final[tuple[str, ...]] = (
    "abcdefghijklmnopqrstuvwxyz",
    "0123456789",
    "qwertyuiop",
    "asdfghjkl",
    "zxcvbnm",
    "йцукенгшщзхї",
    "фівапролджє",
    "ячсмитьбю",
)
_SEQUENCE_RUN: Final = 5


@dataclass(frozen=True, slots=True)
class PolicyResult:
    """Outcome of a strength check.

    ``score`` is 0–4 for the SPA's meter. It is a *display* value derived
    from length and character variety — never the thing that decides
    acceptance, which is ``ok``. Keeping the two apart means the meter
    can be tuned for encouragement without quietly loosening the gate.
    """

    ok: bool
    reasons: tuple[str, ...]
    score: int


def _normalise(value: str) -> str:
    """NFKC + casefold, so visually identical strings compare equal.

    Without NFKC a full-width `ｐａｓｓｗｏｒｄ` would miss the blocklist
    while logging in exactly like the ASCII one, because Keycloak
    normalises on its side.
    """
    return unicodedata.normalize("NFKC", value).casefold()


def _deleet(value: str) -> str:
    return "".join(_LEET.get(ch, ch) for ch in value)


def _is_blocklisted(normalised: str) -> bool:
    """Is this a known-bad password, possibly lightly disguised?

    Four candidates, because the two disguises compose and the ORDER
    they are undone in matters. Stripping the padding must happen on the
    ORIGINAL string, not on the de-leeted one: de-leeting maps digits to
    letters, so `password1234` becomes `passwordl2ea` and the trailing
    junk is no longer trailing junk — it is now letters, and the strip
    finds nothing to remove. Getting that backwards silently let the
    single most common password shape in the world through the gate,
    which is what the test suite caught.
    """
    stripped = re.sub(r"^[\W\d_]+|[\W\d_]+$", "", normalised)
    candidates = {
        normalised,
        _deleet(normalised),
        stripped,
        _deleet(stripped),
    }
    return any(c and c in _COMMON_PASSWORDS for c in candidates)


def _identifier_fragments(*identifiers: str) -> list[str]:
    """Meaningful pieces of the user's own identifiers.

    An email yields its local part and each dotted/hyphenated word, so
    `olena.kovalenko@clinic.example` blocks `olena`, `kovalenko` and
    `clinic` — the three things people actually reach for. Fragments
    under 4 characters are dropped: banning `de` would make a large slice
    of German passphrases unusable for no security gain.
    """
    out: list[str] = []
    for raw in identifiers:
        if not raw:
            continue
        local = _normalise(raw).split("@", 1)[0]
        out.append(local)
        out.extend(re.split(r"[.\-_+\s]+", local))
        domain = _normalise(raw).split("@", 1)[1] if "@" in raw else ""
        if domain:
            out.extend(re.split(r"[.\-_]+", domain)[:1])
    return list({f for f in out if len(f) >= 4})


def _has_sequential_run(value: str) -> bool:
    lowered = _normalise(value)
    for seq in _SEQUENCES:
        reverse = seq[::-1]
        for start in range(len(seq) - _SEQUENCE_RUN + 1):
            window = seq[start : start + _SEQUENCE_RUN]
            if window in lowered or reverse[start : start + _SEQUENCE_RUN] in lowered:
                return True
    return False


def strength_score(password: str) -> int:
    """0–4, for the meter only. Never gates acceptance."""
    if not password:
        return 0
    length = len(password)
    variety = sum(
        (
            any(c.islower() for c in password),
            any(c.isupper() for c in password),
            any(c.isdigit() for c in password),
            any(not c.isalnum() for c in password),
        )
    )
    score = 0
    if length >= 12:
        score += 1
    if length >= 16:
        score += 1
    if length >= 20:
        score += 1
    if variety >= 3:
        score += 1
    # A long single-class passphrase is genuinely strong; a short mixed
    # one is not. Cap rather than reward the mixture on its own.
    if length < 12:
        return 0
    return min(score, 4)


def check_password(
    password: str,
    *,
    min_length: int = 12,
    email: str = "",
    display_name: str = "",
) -> PolicyResult:
    """Evaluate a candidate password.

    ``min_length`` comes from settings but is floored at
    :data:`MIN_LENGTH_FLOOR` — a deployment can raise the bar, never drop
    it below what 800-63B calls the minimum for a memorised secret.
    """
    floor = max(int(min_length), MIN_LENGTH_FLOOR)
    reasons: list[str] = []

    if not password.strip():
        # Checked before length so an all-spaces string gets the specific
        # message rather than "too short" when it is in fact long.
        return PolicyResult(ok=False, reasons=(WHITESPACE_ONLY,), score=0)

    if len(password) < floor:
        reasons.append(TOO_SHORT)
    if len(password) > MAX_LENGTH:
        # An upper bound exists only to stop a megabyte of input reaching
        # the KDF as a denial-of-service; it is not a security control.
        reasons.append(TOO_LONG)

    normalised = _normalise(password)
    if _is_blocklisted(normalised):
        reasons.append(COMMON)

    fragments = _identifier_fragments(email, display_name)
    if any(f in normalised for f in fragments):
        reasons.append(CONTAINS_IDENTIFIER)

    if len(set(password)) <= 2 and len(password) >= 4:
        reasons.append(REPEATED)

    if _has_sequential_run(password):
        reasons.append(SEQUENTIAL)

    ok = not reasons
    return PolicyResult(
        ok=ok,
        reasons=tuple(dict.fromkeys(reasons)),
        score=strength_score(password) if ok else 0,
    )
