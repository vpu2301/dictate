"""Pre-render the letters' display type as PNGs.

WHY AN IMAGE AND NOT A WEBFONT. Gmail and Outlook strip `@font-face`
outright — no origin, no CSP and no amount of correct CSS changes that — so
in the client most of these letters are actually read in, Galgo has never
rendered once. The two places the brand is carried are the wordmark and the
hero headline; rasterising exactly those two, and nothing else, is what makes
the letter open the way klarnote.com does everywhere. Every other line stays
live text in the Geist stack: an email set entirely in images is unreadable
with images off, unselectable, untranslatable and a spam signal.

The images ride as `cid:` related parts, not as links to an origin. That is
deliberate — a hosted image needs a reachable HTTPS host, which is the exact
dependency that left the fonts unrendered in the first place, and it would
also turn every read into a tracking-pixel request against our own domain.

WEIGHTS AND SPACING COME FROM THE SITE, not from the old email CSS: Galgo at
700 (the axis maximum — src/app-extra.css `.lp .lp-brand-name` and `.lp-h1`),
the wordmark at 0.06em tracking, the headline at none. The letter used to ask
for weight 400 at 0.10em because it was never going to get Galgo anyway and
the numbers were tuned against the fallback.

Deterministic: same font, same strings, same PNGs. Regenerate with

    python services/marketing-service/tools/render_mail_images.py

after editing a headline — the strings below MUST match the `{% block
headline %}` in each template, and test_render.py asserts that they do.
"""

from __future__ import annotations

import io
from pathlib import Path

from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).parent
FONT = HERE / "galgo-condensed.woff2"
OUT = HERE.parent / "src" / "marketing_service" / "adapters" / "mail_images"

# Rendered at 2× and published at 1× through the img width attribute: the
# letters are read on retina screens, and a 1× raster of 52px caps looks
# visibly softer than the live text beside it.
SCALE = 2
INK = (27, 36, 34, 255)  # #1B2422 — the site's headline ink
WEIGHT = 700  # the Galgo wght axis maximum; the site never asks for less

# The card's inner width: 640 frame − 48 padding each side. A headline wider
# than this would be scaled down by the mail client and stop matching the
# body type's size, so the generator refuses instead of shipping it.
MAX_WIDTH = 544

WORDMARK = "Klarnote"
WORDMARK_PX = 23
WORDMARK_TRACKING = 0.06

HEADLINE_PX = 52
# 0.92 like `.lp .lp-h1`: Galgo carries its cap at 0.613em, so the leading that
# reads as tight on Geist is loose here.
HEADLINE_LEADING = 0.92
HEADLINE_TRACKING = 0.0

# kind → lang → the headline, one entry per line. Uppercased at render time
# because the template did it with text-transform, and the source strings stay
# readable as sentences for whoever edits them.
HEADLINES: dict[str, dict[str, list[str]]] = {
    "contact_received": {
        "en": ["Thank you —", "your message", "has reached us."],
        "de": ["Danke —", "Ihre Nachricht", "ist angekommen."],
        "uk": ["Дякуємо —", "ваше повідомлення", "в нас."],
    },
    "request_received": {
        "en": ["Thank you —", "we have your request."],
        "de": ["Danke —", "Ihre Anfrage ist da."],
        "uk": ["Дякуємо —", "ваш запит у нас."],
    },
    "demo_confirmed": {
        "en": ["It's booked —", "see you then."],
        "de": ["Gebucht —", "bis dann."],
        "uk": ["Заплановано —", "до зустрічі."],
    },
    "subscribe_confirmed": {
        "en": ["You're on", "the list."],
        "de": ["Sie sind", "dabei."],
        "uk": ["Ви", "в списку."],
    },
}


def load_font(size_px: int) -> ImageFont.FreeTypeFont:
    """Galgo at `size_px`, pinned to the 700 weight.

    Pillow reads TrueType, not WOFF2, so the compressed web file is inflated
    in memory rather than committed twice in two formats — one artefact, one
    licence, no chance of the two drifting.
    """
    tt = TTFont(FONT)
    tt.flavor = None
    buffer = io.BytesIO()
    tt.save(buffer)
    buffer.seek(0)
    font = ImageFont.truetype(buffer, size=size_px * SCALE)
    font.set_variation_by_axes([WEIGHT])
    return font


def draw_tracked(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: str,
    font: ImageFont.FreeTypeFont,
    tracking_px: float,
) -> float:
    """Draw `text` one glyph at a time, adding `tracking_px` between them.

    PIL has no letter-spacing. Condensed capitals need it — this face sets its
    stems 0.198em apart, so KLARNOTE without added air welds into one dark bar
    (the same note as `.lp .lp-brand` in app-extra.css). Returns the advance.
    """
    x, y = xy
    for char in text:
        draw.text((x, y), char, font=font, fill=INK)
        x += font.getlength(char) + tracking_px
    return x - xy[0] - (tracking_px if text else 0)


def measure(text: str, font: ImageFont.FreeTypeFont, tracking_px: float) -> float:
    if not text:
        return 0.0
    return sum(font.getlength(c) for c in text) + tracking_px * (len(text) - 1)


def render(lines: list[str], size_px: int, tracking: float, leading: float) -> Image.Image:
    """Draw onto a deliberately oversized canvas, then crop to the ink.

    Sizing the canvas from the advance widths is what clipped the first
    attempt: the last glyph of a line can carry ink past its own advance (a
    negative right side bearing, which a condensed face with flat-sided caps
    has plenty of), and `Я` lost its leg to the canvas edge. Measuring the
    alpha channel afterwards is exact where arithmetic over metrics is not,
    and cropping to the ink also means the published width is the mark itself
    — no transparent margin the mail client would scale along with it.
    """
    font = load_font(size_px)
    tracking_px = tracking * size_px * SCALE
    line_h = leading * size_px * SCALE
    em = size_px * SCALE

    upper = [line.upper() for line in lines]
    slack = int(em)  # room for bearings, accents and descenders on every side
    canvas_w = int(max(measure(line, font, tracking_px) for line in upper)) + slack * 2
    canvas_h = int(line_h * (len(upper) - 1) + em * 2) + slack * 2

    image = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    for index, line in enumerate(upper):
        draw_tracked(draw, (slack, slack + index * line_h), line, font, tracking_px)

    ink = image.getbbox()
    if ink is None:  # pragma: no cover — an all-whitespace headline
        raise ValueError(f"nothing was drawn for {lines!r}")
    margin = 2
    return image.crop(
        (
            max(0, ink[0] - margin),
            max(0, ink[1] - margin),
            min(canvas_w, ink[2] + margin),
            min(canvas_h, ink[3] + margin),
        )
    )


def save(image: Image.Image, name: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    # optimize + no metadata: these ship in every letter, and a PNG chunk
    # carrying a timestamp would also break the "same inputs, same bytes"
    # property the golden tests rely on.
    image.save(path, format="PNG", optimize=True)
    css_width = round(image.width / SCALE)
    flag = "  ← WIDER THAN THE CARD" if css_width > MAX_WIDTH else ""
    print(f"{name:38} {image.width}×{image.height}px  css {css_width}px{flag}")


def main() -> None:
    save(
        render([WORDMARK], WORDMARK_PX, WORDMARK_TRACKING, 1.0),
        "wordmark.png",
    )
    for kind, langs in HEADLINES.items():
        for lang, lines in langs.items():
            save(
                render(lines, HEADLINE_PX, HEADLINE_TRACKING, HEADLINE_LEADING),
                f"headline.{kind}.{lang}.png",
            )


if __name__ == "__main__":
    main()
