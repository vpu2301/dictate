"""Extraction + garbage detection: prefer skipping a page over ingesting junk."""

from __future__ import annotations

from evidence_websearch.domain.extract import SkipReason, extract

BODY = " ".join(
    [
        "Empiric therapy for community-acquired pneumonia in adults should be "
        "guided by severity assessment and local resistance patterns."
    ]
    * 8
)

GOOD_PAGE = f"""
<html><head><title>CAP guideline — WHO</title></head>
<body>
  <nav><a href="/a">Home</a><a href="/b">Guidelines</a><a href="/c">About</a></nav>
  <header><a href="/x">Skip to content</a></header>
  <main><article>
    <h1>Community-acquired pneumonia</h1>
    <p>{BODY}</p>
    <p>Duration of therapy is five days in most patients who respond.</p>
    <ul><li>Assess severity with a validated score.</li></ul>
  </article></main>
  <aside class="related"><a href="/r1">Related</a><a href="/r2">More</a></aside>
  <footer>© WHO</footer>
</body></html>
"""


def test_extracts_main_content_and_drops_chrome() -> None:
    result = extract(GOOD_PAGE, min_chars=200, max_link_density=0.35)

    assert result.ok, result.skip_reason
    assert "community-acquired pneumonia" in result.text.casefold()
    assert "Duration of therapy" in result.text
    # Navigation, related links and the footer are chrome, not evidence.
    assert "Skip to content" not in result.text
    assert "Related" not in result.text
    assert "© WHO" not in result.text


def test_title_prefers_the_heading_over_the_site_suffixed_title() -> None:
    result = extract(GOOD_PAGE, min_chars=200, max_link_density=0.35)
    assert result.title == "Community-acquired pneumonia"


def test_short_page_is_skipped_as_too_short() -> None:
    page = "<html><body><main><p>Not much here.</p></main></body></html>"
    result = extract(page, min_chars=400, max_link_density=0.35)

    assert not result.ok
    assert result.skip_reason == SkipReason.too_short


def test_link_soup_is_skipped() -> None:
    """A directory/index page: plenty of characters, almost all of them links."""
    links = "".join(
        f'<p><a href="/guideline/{i}">Guideline number {i} about clinical care</a></p>'
        for i in range(60)
    )
    page = f"<html><body><main>{links}</main></body></html>"

    result = extract(page, min_chars=200, max_link_density=0.35)

    assert not result.ok
    assert result.skip_reason == SkipReason.link_soup


def test_paywall_interstitial_is_detected_not_ingested() -> None:
    page = f"""
    <html><body><main>
      <h1>Effect of therapy X</h1>
      <p>Abstract: {BODY}</p>
      <p>Subscribe to continue reading this article.</p>
    </main></body></html>
    """
    result = extract(page, min_chars=200, max_link_density=0.9)

    assert not result.ok
    assert result.skip_reason == SkipReason.paywall


def test_javascript_wall_is_detected() -> None:
    page = """
    <html><body><main>
      <p>Please enable JavaScript to view this content.</p>
    </main></body></html>
    """
    result = extract(page, min_chars=100, max_link_density=0.9)

    assert not result.ok
    assert result.skip_reason == SkipReason.consent_wall


def test_page_with_no_content_node_is_skipped() -> None:
    result = extract("<html></html>", min_chars=100, max_link_density=0.9)
    assert not result.ok
    assert result.skip_reason in (SkipReason.no_content, SkipReason.too_short)


def test_extraction_is_deterministic() -> None:
    """Two runs over the same bytes must produce identical text: the chunk
    offsets stored with a citation depend on it."""
    first = extract(GOOD_PAGE, min_chars=200, max_link_density=0.35)
    second = extract(GOOD_PAGE, min_chars=200, max_link_density=0.35)
    assert first.text == second.text


def test_nested_boilerplate_does_not_crash_the_extractor() -> None:
    """Regression: every real page has chrome inside chrome.

    `find_all` hands back a snapshot, so decomposing the outer wrapper destroys
    inner nodes that are still in the list being walked. Reading a destroyed
    tag's attributes raises AttributeError, the connector logs `page_failed`,
    and the page is lost — which is how cdc.gov and who.int were dropped from
    every answer while the fetch itself succeeded.
    """
    page = f"""
    <html><head><title>Atrial fibrillation — CDC</title></head>
    <body>
      <div class="sidebar">
        <div class="related"><a href="/r1">Related</a></div>
        <div id="cookie-banner"><p>We use cookies on this site.</p></div>
      </div>
      <main><article>
        <h1>Atrial fibrillation</h1>
        <p>{BODY}</p>
      </article></main>
    </body></html>
    """
    result = extract(page, min_chars=200, max_link_density=0.35)

    assert result.ok, result.skip_reason
    assert result.title == "Atrial fibrillation"
    assert "Related" not in result.text
    assert "We use cookies" not in result.text
