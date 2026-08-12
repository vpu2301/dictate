"""Which of the three templates a given request gets.

The rule the emails have to satisfy is "answer in the language the query
came from". Three signals can tell us that and they disagree often
enough that the ORDER matters more than any one of them:

1. **The language the form was in.** The strongest signal by a wide
   margin, because it is a choice the visitor made on our own site —
   a Ukrainian doctor reading the German page wants German back.
2. **The country the request came from.** A CDN geo header, or the
   country the form collected. Right far more often than not, but it is
   an inference: a Kyiv clinician on holiday in Munich is not German.
3. **`Accept-Language`.** Last because it reflects how the browser was
   installed, which for a large share of Ukrainian users is `en-US` on
   a machine whose owner does not read English comfortably.

Anything unresolved falls to English rather than to the biggest market.
An English mail to a Ukrainian reader is a small friction; a Ukrainian
mail to an Austrian buyer looks like it was meant for someone else.

Pure functions, no I/O — the whole point is that the choice can be
asserted in a unit test rather than inferred from a rendered mail.
"""

from __future__ import annotations

from typing import Final

# The languages we actually have designed templates for. Adding a fourth
# means adding a template file, so this tuple and the template directory
# are checked against each other by a test.
SUPPORTED: Final[tuple[str, ...]] = ("en", "de", "uk")
DEFAULT: Final = "en"

# Country → template language. Only entries that change the answer are
# listed; everything absent is English by the fallback rule above.
#
# DE/AT/CH are the German-speaking market the German template was written
# for. CH is included knowingly: it is also French- and Italian-speaking,
# but German is its largest language and we have no French template to
# offer as the better answer.
_COUNTRY_TO_LANG: Final[dict[str, str]] = {
    "UA": "uk",
    "DE": "de",
    "AT": "de",
    "CH": "de",
}


def normalise_lang(raw: str | None) -> str | None:
    """`de-AT`, `DE_de`, ` De ` → `de`. Unsupported tags → None.

    Returns None rather than the default so a caller can distinguish
    "the visitor asked for something we do not have" from "the visitor
    did not ask", and fall through to the next signal instead of
    stopping at English.
    """
    if not raw:
        return None
    tag = raw.strip().lower().replace("_", "-")
    primary = tag.split("-", 1)[0]
    return primary if primary in SUPPORTED else None


def normalise_country(raw: str | None) -> str | None:
    """ISO-3166-1 alpha-2, upper case. Anything else → None."""
    if not raw:
        return None
    code = raw.strip().upper()
    return code if len(code) == 2 and code.isalpha() else None


def lang_from_country(country: str | None) -> str | None:
    code = normalise_country(country)
    return _COUNTRY_TO_LANG.get(code) if code else None


def parse_accept_language(header: str | None) -> list[str]:
    """`uk-UA,uk;q=0.9,en;q=0.8` → ['uk', 'uk', 'en'], best first.

    Malformed q-values sort last rather than raising: a header is
    attacker-controlled input, and a 500 on the demo form because
    someone sent `q=;` would be a self-inflicted outage.
    """
    if not header:
        return []
    scored: list[tuple[float, int, str]] = []
    for index, part in enumerate(header.split(",")):
        bits = part.split(";")
        tag = bits[0].strip()
        if not tag or tag == "*":
            continue
        quality = 1.0
        for extra in bits[1:]:
            key, _, value = extra.partition("=")
            if key.strip().lower() != "q":
                continue
            try:
                quality = float(value.strip())
            except ValueError:
                quality = 0.0
        # `index` keeps the header's own order as the tie-break, which is
        # what the spec says equal-q tags mean.
        scored.append((-quality, index, tag))
    return [tag for _, _, tag in sorted(scored)]


def resolve_language(
    *,
    form_lang: str | None = None,
    country: str | None = None,
    accept_language: str | None = None,
) -> str:
    """The one entry point. Always returns a member of SUPPORTED."""
    from_form = normalise_lang(form_lang)
    if from_form:
        return from_form

    from_country = lang_from_country(country)
    if from_country:
        return from_country

    for tag in parse_accept_language(accept_language):
        candidate = normalise_lang(tag)
        if candidate:
            return candidate

    return DEFAULT
