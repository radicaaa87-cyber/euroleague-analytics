"""Automated verification of the R-11 public launch package for euroleague.egemenyucelen.me.

Validates:
1. Static site files structure, valid HTML5, and clean internal links.
2. Zero-tracking policy: no analytics scripts, pixels, external CDNs, or cookie trackers.
3. CNAME file correctness for custom domain euroleague.egemenyucelen.me.
4. Consistency of verified project numbers across README, site, and sponsor brief.
5. Accurate disclosures in privacy policy regarding durable row budget and third-party providers.
"""

from __future__ import annotations

import base64
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

from euroleague.mcp.tools import TOOL_NAMES

SITE_DIR = Path("site")
# The Turkish page lives one directory down, so every relative reference in it
# climbs with "../". The checks below resolve each reference against the page's
# own directory and then require the result to stay inside site/.
HTML_FILES = ("index.html", "privacy.html", "support.html", "chatgpt/index.html", "tr/index.html")
TURKISH_PAGE = SITE_DIR / "tr" / "index.html"


def _resolve_inside_site(page: str, reference: str) -> Path:
    """Where a relative href or src in `page` lands, or fail if it leaves site/."""
    bare = reference.split("#", 1)[0].split("?", 1)[0]
    target = ((SITE_DIR / page).parent / bare).resolve()
    assert target.is_relative_to(SITE_DIR.resolve()), (
        f"In {page}: '{reference}' resolves outside the site directory to {target}"
    )
    return target


DOC_FILES = ("SPONSOR_ONE_PAGER.md", "LAUNCH_COPY.md", "OWNER_LAUNCH_STEPS.md")
LAUNCH_THREAD = Path("docs/LAUNCH_THREAD_FINAL.md")
CHATGPT_SUBMISSION_RECORD = Path("docs/CHATGPT_APP_SUBMISSION.md")
PUBLIC_LAUNCH_SURFACES = (
    Path("README.md"),
    SITE_DIR / "index.html",
    SITE_DIR / "support.html",
    Path("docs/LAUNCH_COPY.md"),
    Path("docs/SPONSOR_ONE_PAGER.md"),
    LAUNCH_THREAD,
)
ARCHIVE_STATUS_SURFACES = (
    Path("README.md"),
    SITE_DIR / "index.html",
    Path("docs/LAUNCH_COPY.md"),
    Path("docs/SPONSOR_ONE_PAGER.md"),
)
# Surfaces that must not overclaim, but are not required to raise the subject.
# A tweet thread has no room to disclose the backfill; it still must not say the
# archive is finished.
OVERCLAIM_SURFACES = (*ARCHIVE_STATUS_SURFACES, SITE_DIR / "support.html", LAUNCH_THREAD)

FORBIDDEN_TRACKER_PATTERNS = [
    r"google-analytics\.com",
    r"googletagmanager\.com",
    r"analytics\.js",
    r"gtag",
    r"facebook\.net",
    r"hotjar\.com",
    r"segment\.com",
    r"mixpanel\.com",
    r"clarity\.ms",
]


class LinkExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self.scripts: list[str] = []
        self.stylesheets: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_dict = {k.lower(): (v or "") for k, v in attrs}
        if tag == "a" and "href" in attr_dict:
            self.links.append(attr_dict["href"])
        elif tag == "link" and attr_dict.get("rel") == "stylesheet" and "href" in attr_dict:
            self.stylesheets.append(attr_dict["href"])
        elif tag == "script" and "src" in attr_dict:
            self.scripts.append(attr_dict["src"])


class PrivacyMarkupParser(HTMLParser):
    """Extract structured strong-tagged labels and code fragments from privacy policy."""

    def __init__(self) -> None:
        super().__init__()
        self.strong_labels: list[str] = []
        self.code_elements: list[str] = []
        self._in_strong = False
        self._in_code = False
        self._current_strong: list[str] = []
        self._current_code: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag == "strong":
            self._in_strong = True
            self._current_strong = []
        elif tag == "code":
            self._in_code = True
            self._current_code = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "strong":
            self._in_strong = False
            label = "".join(self._current_strong).strip().rstrip(":")
            if label:
                self.strong_labels.append(label)
        elif tag == "code":
            self._in_code = False
            code_text = "".join(self._current_code).strip()
            if code_text:
                self.code_elements.append(code_text)

    def handle_data(self, data: str) -> None:
        if self._in_strong:
            self._current_strong.append(data)
        if self._in_code:
            self._current_code.append(data)


def test_site_directory_and_required_files_exist() -> None:
    """All required static site assets must exist on disk."""
    assert SITE_DIR.is_dir(), "site/ directory is missing"
    assert (SITE_DIR / "style.css").is_file(), "site/style.css is missing"
    assert (SITE_DIR / "CNAME").is_file(), "site/CNAME is missing"
    for filename in HTML_FILES:
        path = SITE_DIR / filename
        assert path.is_file(), f"site/{filename} is missing"


def test_cname_file_contains_expected_domain() -> None:
    """CNAME file must specify euroleague.egemenyucelen.me exactly."""
    cname_text = (SITE_DIR / "CNAME").read_text(encoding="utf-8").strip()
    expected = "euroleague.egemenyucelen.me"
    assert cname_text == expected, f"Expected '{expected}', got '{cname_text}'"


def test_chatgpt_submission_record_uses_product_subdomain() -> None:
    """Portal-facing public URLs must use the product subdomain and app landing page."""
    content = CHATGPT_SUBMISSION_RECORD.read_text(encoding="utf-8")
    base_url = "https://euroleague.egemenyucelen.me"
    assert f"**Website URL:** `{base_url}/chatgpt/`" in content
    assert f"**Support / Terms URL:** `{base_url}/support.html`" in content
    assert f"**Privacy Policy URL:** `{base_url}/privacy.html`" in content
    assert f"**Demo Recording:** `{base_url}/launch-film.mp4`" in content


def test_chatgpt_submission_brand_is_independent_and_competition_descriptive() -> None:
    """Directory metadata and landing copy avoid presenting a competition as the app brand."""
    import json

    manifest = json.loads(Path("chatgpt-app-submission.json").read_text(encoding="utf-8"))
    app_info = manifest["app_info"]
    landing = (SITE_DIR / "chatgpt" / "index.html").read_text(encoding="utf-8")

    assert app_info["display_name"] == "European Basketball Analytics"
    assert app_info["subtitle"] == "Advanced European basketball analytics"
    assert "EuroLeague" in app_info["description"]
    assert "EuroCup" in app_info["description"]
    assert "European Basketball Analytics" in landing
    assert "Independent analytics project." in landing
    disclaimer = "Not affiliated with or endorsed by Euroleague Basketball or its competitions."
    assert disclaimer in landing


def test_html_files_have_valid_html5_structure() -> None:
    """Every HTML file must have valid HTML5 declarations and essential meta tags."""
    for filename in HTML_FILES:
        content = (SITE_DIR / filename).read_text(encoding="utf-8")
        assert "<!DOCTYPE html>" in content, f"{filename} missing <!DOCTYPE html>"
        assert "<html" in content and "</html>" in content, f"{filename} missing <html> tags"
        assert "<head>" in content and "</head>" in content, f"{filename} missing <head> tags"
        assert '<meta charset="UTF-8">' in content, f"{filename} missing UTF-8 charset"
        assert '<meta name="viewport"' in content, f"{filename} missing viewport meta tag"
        assert "<title>" in content and "</title>" in content, f"{filename} missing <title>"
        # The Turkish page's <body> carries data-text attributes, so the tag
        # is matched by name rather than as a bare "<body>".
        assert re.search(r"<body[\s>]", content) and "</body>" in content, (
            f"{filename} missing <body> tags"
        )


def test_internal_links_and_stylesheets_resolve() -> None:
    """All relative links and stylesheet links inside HTML files must point to existing files."""
    for filename in HTML_FILES:
        html_path = SITE_DIR / filename
        content = html_path.read_text(encoding="utf-8")
        parser = LinkExtractor()
        parser.feed(content)

        # Check stylesheets
        for sheet_href in parser.stylesheets:
            target = _resolve_inside_site(filename, sheet_href)
            assert target.is_file(), (
                f"In {filename}: stylesheet '{sheet_href}' not found at {target}"
            )

        # Check relative hyperlinks
        for href in parser.links:
            parsed = urlparse(href)
            if parsed.scheme or href.startswith("#"):
                continue
            # Strip fragment and query, e.g. "index.html#features" -> "index.html"
            base_href = href.split("#", 1)[0].split("?", 1)[0]
            if base_href:
                target = _resolve_inside_site(filename, href)
                # "../" from the Turkish page names the site root; the root is
                # served as index.html.
                if target.is_dir():
                    target = target / "index.html"
                assert target.is_file(), (
                    f"In {filename}: broken relative link '{href}' (resolves to {target})"
                )


def test_external_links_use_https() -> None:
    """All external web links must use HTTPS."""
    for filename in HTML_FILES:
        content = (SITE_DIR / filename).read_text(encoding="utf-8")
        parser = LinkExtractor()
        parser.feed(content)
        for href in parser.links:
            parsed = urlparse(href)
            if parsed.scheme == "http":
                raise AssertionError(f"In {filename}: insecure HTTP link found: '{href}'")


def test_zero_trackers_and_third_party_scripts() -> None:
    """The site must load no script it does not ship itself.

    This used to forbid every `<script src>` outright, which matched the old
    site because the old site had no behaviour. The rebuilt hero types a
    conversation and the shot chart places real coordinates, so first-party
    scripts now exist and that blanket ban would have to be deleted rather than
    tightened - the worst outcome, because the property actually worth keeping
    is not "no scripts" but "nothing from anybody else".

    So the rule is now what its name always said: a script must be a relative
    path inside `site/`. Any absolute URL, protocol-relative URL, or path that
    climbs out of the directory fails, whatever host it names. A visitor's
    browser talks to this site and to nothing else - no CDN, no analytics, no
    font host, nobody who could log the visit.
    """
    for filename in HTML_FILES:
        content = (SITE_DIR / filename).read_text(encoding="utf-8")
        for pattern in FORBIDDEN_TRACKER_PATTERNS:
            match = re.search(pattern, content, re.IGNORECASE)
            assert not match, (
                f"In {filename}: forbidden tracker pattern '{pattern}' matched: {match.group(0)}"
            )

        parser = LinkExtractor()
        parser.feed(content)
        for script_src in parser.scripts:
            assert not re.match(r"(?:[a-z][a-z0-9+.-]*:)?//", script_src, re.IGNORECASE), (
                f"In {filename}: script is loaded from another origin: {script_src}"
            )
            assert not script_src.startswith("/"), (
                f"In {filename}: script escapes the site directory: {script_src}"
            )
            assert _resolve_inside_site(filename, script_src).is_file(), (
                f"In {filename}: script '{script_src}' is not shipped with the site"
            )


def test_the_turkish_page_is_reached_by_redirect_and_can_always_be_left() -> None:
    """Decision 53: a Turkish browser is sent to /tr/, and nobody is trapped there.

    What this checks: the English page carries the redirect, the redirect only
    fires for a Turkish browser language, it stands down when the visitor has
    chosen English, and each page names the other for search engines and for
    the visitor. What it cannot check: that a real browser takes the redirect,
    which needs a browser with its language set to Turkish.
    """
    index_text = (SITE_DIR / "index.html").read_text(encoding="utf-8")
    turkish_text = TURKISH_PAGE.read_text(encoding="utf-8")

    assert '<html lang="tr">' in turkish_text
    assert '<html lang="en">' in index_text

    # The redirect: inline, before anything renders, and conditional.
    head = index_text.split("</head>", 1)[0]
    assert "navigator.language" in head, "index.html must read the browser language"
    assert "tr/" in head, "the redirect must name the Turkish page"
    assert "lang=en" in head, "a visitor who asked for English must not be redirected"
    assert "navigator.language" not in turkish_text, (
        "the Turkish page must never redirect; it is where the redirect lands"
    )

    # Each page tells search engines about the other.
    for page_text, page_name in ((index_text, "index.html"), (turkish_text, "tr/index.html")):
        assert 'hreflang="en"' in page_text, f"{page_name} lacks the English alternate"
        assert 'hreflang="tr"' in page_text, f"{page_name} lacks the Turkish alternate"
        assert 'hreflang="x-default"' in page_text, f"{page_name} lacks the default alternate"

    # The way out, in both directions. The English link carries lang=en so the
    # redirect does not immediately send the visitor back.
    assert "../?lang=en" in turkish_text or "../index.html?lang=en" in turkish_text, (
        "the Turkish page needs a link back to English that disarms the redirect"
    )
    assert 'href="tr/"' in index_text, "the English page needs a quiet link to Turkish"


def test_the_turkish_page_shares_the_english_page_s_assets_and_claims() -> None:
    """The Turkish page is the same product, so it loads the same scripts and figures.

    Authored copy may differ sentence by sentence; the scripts, the recordings
    and the one coverage figure may not. A script listed on one page and not
    the other means one language gets a broken section.
    """
    index_text = (SITE_DIR / "index.html").read_text(encoding="utf-8")
    turkish_text = TURKISH_PAGE.read_text(encoding="utf-8")

    def scripts_of(text: str) -> set[str]:
        parser = LinkExtractor()
        parser.feed(text)
        return {Path(src).name for src in parser.scripts}

    assert scripts_of(turkish_text) == scripts_of(index_text)

    for section_id in ("film", "shots", "lineups", "connect", "deep", "how"):
        assert f'id="{section_id}"' in turkish_text, f"Turkish page lacks section {section_id}"

    assert "732" in turkish_text, "the Turkish page must state the same games-loaded figure"
    assert "https://euroleague-analytics-mcp.fly.dev/mcp" in turkish_text

    # The recordings are the same files, except the launch film which has a
    # dedicated Turkish cut (Decision 83).
    for media in ("hero-demo.mp4", "hard-1.mp4", "hard-2.mp4", "hard-3.mp4"):
        assert f"../{media}" in turkish_text, f"Turkish page does not reuse {media}"
    assert "../launch-film-tr.mp4" in turkish_text, (
        "Turkish page does not use localized launch film"
    )


def test_the_turkish_page_carries_every_sentence_the_scripts_can_show() -> None:
    """Decision 53: scripts hold no Turkish, so the page must supply each string.

    The scripts read `data-text-<key>` from <body> and fall back to English.
    A key the Turkish page forgets shows an English sentence in the middle of
    a Turkish page, silently. This test lists the keys from the scripts
    themselves, so a new key added to a script without its Turkish text fails
    here rather than on the page.
    """
    keys: set[str] = set()
    for script in SITE_DIR.glob("*.js"):
        script_text = script.read_text(encoding="utf-8")
        keys.update(re.findall(r'text\("([a-z]+(?:-[a-z]+)*)"', script_text))
        keys.update(re.findall(r'data-text-([a-z]+(?:-[a-z]+)*)"', script_text))
    # The position words are looked up by the data's values, not by a literal.
    keys.update({"position-guard", "position-forward", "position-center"})
    assert keys, "no data-text keys found in the scripts; the lookup has moved"

    body_tag = re.search(r"<body[^>]*>", TURKISH_PAGE.read_text(encoding="utf-8"))
    assert body_tag is not None
    for key in sorted(keys):
        assert f'data-text-{key}="' in body_tag.group(0), (
            f"tr/index.html <body> lacks data-text-{key}; a script would show English there"
        )


def test_site_scripts_locate_their_data_from_their_own_address() -> None:
    """A script served to /tr/ must still find site/data/, so the path is not page-relative.

    fetch("data/x.json") resolves against the page, and from /tr/ that is
    /tr/data/x.json, which does not exist. Resolving against the script's own
    URL gives the same answer from every page that loads it.
    """
    for name in ("shots.js", "lineups.js"):
        text = (SITE_DIR / name).read_text(encoding="utf-8")
        assert 'fetch("data/' not in text, f"{name} still fetches relative to the page"
        assert "document.currentScript" in text, f"{name} does not resolve data from its own URL"


def test_launch_documentation_files_exist() -> None:
    """Sponsor one-pager, launch copy, and owner checklist must exist."""
    sponsor_doc = Path("docs/SPONSOR_ONE_PAGER.md")
    launch_copy = Path("docs/LAUNCH_COPY.md")
    owner_steps = Path("docs/OWNER_LAUNCH_STEPS.md")

    assert sponsor_doc.is_file(), "docs/SPONSOR_ONE_PAGER.md is missing"
    assert launch_copy.is_file(), "docs/LAUNCH_COPY.md is missing"
    assert owner_steps.is_file(), "docs/OWNER_LAUNCH_STEPS.md is missing"

    # Verify non-trivial length
    assert len(sponsor_doc.read_text(encoding="utf-8").splitlines()) >= 30
    assert len(launch_copy.read_text(encoding="utf-8").splitlines()) >= 50
    assert len(owner_steps.read_text(encoding="utf-8").splitlines()) >= 40


def test_privacy_policy_accurately_discloses_durable_row_budget_and_providers() -> None:
    """Privacy policy must describe durable database row ledger and real third-party providers."""
    privacy_text = (SITE_DIR / "privacy.html").read_text(encoding="utf-8")
    parser = PrivacyMarkupParser()
    parser.feed(privacy_text)

    # Durable row budget table and identifier disclosure in code markup
    assert "public.mcp_row_usage" in parser.code_elements, (
        "privacy.html missing code markup for public.mcp_row_usage"
    )
    assert "sub" in parser.code_elements, "privacy.html missing code markup for sub"

    # Structured provider items in Section 5
    required_providers = {
        "Auth0 / Google",
        "Fly.io",
        "Supabase (PostgreSQL)",
        "GitHub Pages",
        "Cloudflare",
    }
    present_strong_labels = set(parser.strong_labels)
    missing = required_providers - present_strong_labels
    assert not missing, f"privacy.html missing structured provider labels: {missing}"


def test_verified_claims_consistency() -> None:
    """A figure quoted anywhere must be the verified one, and must not disagree.

    This used to require every surface to carry all four figures, which was a
    reasonable proxy while the site was a page of claims. The rebuilt site
    deliberately has no statistics row - a visitor who does not yet know what
    the product is cannot be moved by "107,311 possessions" - so requiring the
    figure would force copy back onto the page that the design removed.

    What still matters, and is what this now checks: nobody may quote a
    different number. Each figure is optional per surface and exact where it
    appears, so a stale 730 or 99.4% fails wherever somebody writes it.
    """
    readme_text = Path("README.md").read_text(encoding="utf-8")
    index_text = (SITE_DIR / "index.html").read_text(encoding="utf-8")
    sponsor_text = Path("docs/SPONSOR_ONE_PAGER.md").read_text(encoding="utf-8")

    # (verified value, the pattern that would be a competing claim)
    verified = (
        ("732", re.compile(r"7[0-9]{2} games", re.IGNORECASE)),
        ("107,311", re.compile(r"10[0-9],[0-9]{3} possessions", re.IGNORECASE)),
        ("41,524", re.compile(r"4[0-9],[0-9]{3} (?:verified )?coordinates", re.IGNORECASE)),
        ("99.54%", re.compile(r"99\.[0-9]{1,2}%", re.IGNORECASE)),
    )

    # The two documents whose job is to state the numbers still must state them.
    for name, text in (("README", readme_text), ("SPONSOR_ONE_PAGER", sponsor_text)):
        for value, _competing in verified:
            assert value in text, f"{name} missing {value}"

    # The site may say as much or as little as the design calls for, but a
    # figure it does state has to be the verified one.
    for value, competing in verified:
        for match in competing.finditer(index_text):
            assert value in match.group(0), (
                f"index.html quotes {match.group(0)!r}, which is not the verified {value}"
            )

    assert "732" in index_text, (
        "index.html should still say how many games are loaded; it is the one "
        "coverage claim the page makes"
    )


def test_public_launch_copy_does_not_claim_the_running_archive_is_complete() -> None:
    """The historical chain is still running, so completion copy would be false."""
    # Archived is not loaded. E2007 to E2021 are in the immutable archive; the
    # warehouse a visitor queries holds E2024, E2025 and the filling E2026, and
    # copy that blurs the two promises data nobody can query.
    forbidden = (
        # 23 was the season count before 2026-09-03, when E2006 and older were
        # measured as empty at every game endpoint. Twenty is the true figure,
        # and a public surface still saying 23 is selling four seasons that do
        # not exist upstream.
        re.compile(r"23\s+seasons", re.IGNORECASE),
        # Archived is not loaded. E2007 to E2021 sit in the immutable archive;
        # the warehouse a visitor queries holds E2024, E2025 and the filling
        # E2026, and copy that blurs the two promises data nobody can query.
        re.compile(r"every\s+season.{0,30}(?:loaded|queryable)", re.IGNORECASE),
        re.compile(r"all\s+seasons.{0,30}(?:loaded|queryable)", re.IGNORECASE),
    )

    # Until 2026-09-03 every one of these surfaces had to disclose that the
    # archive backfill was still running. It is not: the chain reached the
    # oldest season the API serves and stopped (Decision 52). Requiring the
    # disclosure now would require the page to describe something that is over,
    # so the requirement is dropped and only the overclaim check below stays.

    for path in OVERCLAIM_SURFACES:
        text = path.read_text(encoding="utf-8")
        for pattern in forbidden:
            assert not pattern.search(text), (
                f"{path} overclaims archive completion: {pattern.pattern}"
            )


def test_public_launch_surfaces_only_advertise_current_mcp_tool_names() -> None:
    """Every concrete el_* name in public copy must exist in the live registry."""
    advertised: dict[Path, set[str]] = {}
    current_names = set(TOOL_NAMES)

    for path in PUBLIC_LAUNCH_SURFACES:
        names = set(re.findall(r"\bel_[a-z_]+\b", path.read_text(encoding="utf-8")))
        unknown = names - current_names
        if unknown:
            advertised[path] = unknown

    assert not advertised, f"Public launch copy advertises unknown MCP tools: {advertised}"


def test_readme_tool_table_lists_every_registered_tool() -> None:
    """The README states a tool count, so its table must match the registry.

    The guard above catches a name we advertise but do not serve. It cannot
    catch the opposite - a tool we serve and forgot to list - because an
    incomplete list contains no unknown name. Both directions mislead a reader
    on a public surface, so this asserts the missing one.
    """
    readme_text = Path("README.md").read_text(encoding="utf-8")

    listed = set(re.findall(r"\|\s*`(el_[a-z_]+)`\s*\|", readme_text))
    missing = set(TOOL_NAMES) - listed
    assert not missing, f"README tool table omits registered tools: {sorted(missing)}"

    claimed = re.search(r"exposes (\d+) read-only tools", readme_text)
    assert claimed is not None, "README must state how many read-only tools it exposes"
    assert int(claimed.group(1)) == len(TOOL_NAMES), (
        f"README claims {claimed.group(1)} tools; the registry serves {len(TOOL_NAMES)}"
    )


TOOL_COUNT_SURFACES = (
    Path("README.md"),
    Path("docs/SCOPE.md"),
    Path("docs/CLIENT_COMPATIBILITY.md"),
    Path("docs/SPONSOR_ONE_PAGER.md"),
    Path("docs/LAUNCH_COPY.md"),
    Path("docs/LAUNCH_NARRATIVE.md"),
    SITE_DIR / "index.html",
    SITE_DIR / "support.html",
    SITE_DIR / "motion.js",
    SITE_DIR / "tr" / "index.html",
)

# English number words that could plausibly stand in front of "tool(s)". Any of
# these other than the word for the current registry size is a stale claim -
# Decision 65 froze the count at eleven and Decision 79 moved it to fourteen,
# so "eleven tools" is exactly the kind of leftover this guards against.
_ENGLISH_NUMBER_WORDS = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
)
# Turkish number words that could stand in front of the Turkish word for
# "tool" - same idea as _ENGLISH_NUMBER_WORDS above, for site/tr/index.html.
# Base64-encoded, in the same style tests/test_english_only.py uses for its
# own Turkish word list, so this file's source stays English-only per
# CLAUDE.md even though it has to check Turkish copy. Comment gives the
# English meaning of each entry rather than repeating the Turkish word.
_TURKISH_NUMBER_WORDS_B64 = (
    "b24gYmly",  # eleven
    "b24gaWtp",  # twelve
    "b24gw7zDpw==",  # thirteen
    "b24gZMO2cnQ=",  # fourteen - the current registry size
    "b24gYmXFnw==",  # fifteen
    "b24gYWx0xLE=",  # sixteen
    "b24geWVkaQ==",  # seventeen
    "b24gc2VraXo=",  # eighteen - the current registry size
    "Ymly",  # one
    "aWtp",  # two
    "w7zDpw==",  # three
    "ZMO2cnQ=",  # four
    "YmXFnw==",  # five
)
_TURKISH_TOOL_WORD_B64 = "YXJhw6c="  # the noun this check counts


def test_public_copy_states_the_current_tool_count() -> None:
    """No public surface may quote a tool count that is not the registry's own.

    Derives the expected count from ``euroleague.mcp.tools.TOOL_NAMES`` rather
    than hard-coding it, so this test does not itself need editing the next
    time a tool is added under Decision 65's condition. It catches a stale
    number word ("eleven"), a stale digit form ("11 tools"), and the Turkish
    compound-number equivalent - the three shapes the final review found
    still living in public copy after Decision 79. The regexes require the
    number to sit immediately in front of "tool(s)" or its Turkish equivalent
    (with an optional "read-only"/"MCP" in between on the English side) so an
    unrelated count elsewhere in the same file - a game count, a possession
    count - is not flagged.
    """
    current_count = len(TOOL_NAMES)
    current_word_en = _ENGLISH_NUMBER_WORDS[current_count]
    stale_words_en = [w for w in _ENGLISH_NUMBER_WORDS if w != current_word_en]
    turkish_number_words = [base64.b64decode(w).decode("utf-8") for w in _TURKISH_NUMBER_WORDS_B64]
    tool_word_tr = base64.b64decode(_TURKISH_TOOL_WORD_B64).decode("utf-8")
    current_word_tr = turkish_number_words[7]  # "eighteen" - see the comment above
    stale_words_tr = [w for w in turkish_number_words if w != current_word_tr]

    word_pattern_en = re.compile(
        r"\b(" + "|".join(stale_words_en) + r")\s+(?:read-only\s+|MCP\s+)?tools?\b",
        re.IGNORECASE,
    )
    digit_pattern_en = re.compile(
        r"\b(?!" + str(current_count) + r"\b)(\d{1,2})\s+(?:read-only\s+|MCP\s+)?tools?\b"
    )
    # A bare single-digit word is also the tail of the compound "ten <word>"
    # (the teens), so a lookbehind keeps a bare match from firing on the
    # second half of the correct compound number.
    word_pattern_tr = re.compile(
        r"(?<!on )\b(" + "|".join(re.escape(w) for w in stale_words_tr) + r")\s+" + tool_word_tr
    )

    offenders: list[str] = []
    for path in TOOL_COUNT_SURFACES:
        text = path.read_text(encoding="utf-8")
        for pattern in (word_pattern_en, digit_pattern_en, word_pattern_tr):
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                offenders.append(f"{path}:{line}: {match.group(0)!r}")

    assert not offenders, (
        f"Stale tool-count wording found (registry now has {current_count} tools, "
        f"{current_word_en!r}/{current_word_tr!r}):\n" + "\n".join(offenders)
    )


def test_lineup_metrics_share_the_copy_column() -> None:
    """The live figures must use the space below the short lineup explanation."""
    index_text = (SITE_DIR / "index.html").read_text(encoding="utf-8")
    lineup = index_text.split('id="lineups"', 1)[1].split('id="ask"', 1)[0]

    copy_start = lineup.index('<div class="claim-copy">')
    figure_start = lineup.index('<div class="claim-figure">')
    verdict = lineup.index('<div class="unit-verdict">')

    assert copy_start < verdict < figure_start, (
        "The lineup metrics must sit below the copy instead of adding height under the court"
    )


def test_hard_questions_show_thinking_without_technical_call_chrome() -> None:
    """Each case gets one human-readable thought, not an internal tool transcript."""
    index_text = (SITE_DIR / "index.html").read_text(encoding="utf-8")
    deep = index_text.split('id="deep"', 1)[1].split('id="how"', 1)[0]

    assert deep.count('class="deep-thought"') == 3
    for technical_class in ("deep-call", "toolcall-name", "deep-args", "deep-back"):
        assert technical_class not in deep, (
            f"The hard-question examples still expose technical UI: {technical_class}"
        )


def test_page_background_keeps_only_the_sideline_system() -> None:
    """The page frame keeps its rails and section ticks, without court furniture."""
    index_text = (SITE_DIR / "index.html").read_text(encoding="utf-8")
    background = index_text.split('<div class="courtgrid"', 1)[1].split('<main id="main">', 1)[0]
    stylesheet = (SITE_DIR / "style.css").read_text(encoding="utf-8")

    assert 'class="rail rail-left"' in background
    assert 'class="rail rail-right"' in background
    assert "<svg" not in background
    assert "halfway" not in background
    assert ".claim::before" in stylesheet
