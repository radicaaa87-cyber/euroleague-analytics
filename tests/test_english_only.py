"""Enforce English-only rule across all tracked repository files."""

from __future__ import annotations

import base64
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Turkish-specific characters:
# U+011F (g-breve), U+011E (G-breve)
# U+0131 (dotless i), U+0130 (dotted capital I)
# U+015F (s-cedilla), U+015E (S-cedilla)
TURKISH_SPECIFIC_CHARS = set("\u011f\u0131\u015f\u011e\u0130\u015e")

# Common Turkish words matched on word boundaries (\b...\b), case-insensitively.
# The list is encoded in base64 so this test file contains no raw Turkish words in source.
_B64_TURKISH_WORDS = [
    "Ymly",
    "dmU=",
    "YnU=",
    "acOnaW4=",
    "aWNpbg==",
    "aWxl",
    "ZGXEn2ls",
    "ZGVnaWw=",
    "w6dvaw==",
    "Y29r",
    "YW1h",
    "dmV5YQ==",
    "ZGFoYQ==",
    "a2FkYXI=",
    "b2xhcmFr",
    "Z2liaQ==",
    "b2xhbg==",
    "c29ucmE=",
    "dGFyYWbEsW5kYW4=",
    "dGFyYWZpbmRhbg==",
    "aGF5xLFy",
    "aGF5aXI=",
    "bmFzxLFs",
    "bmFzaWw=",
    "bmVkZW4=",
    "bmnDp2lu",
    "bmljaW4=",
    "YnVyYWRh",
    "xZ9pbWRp",
    "c2ltZGk=",
    "w6fDvG5rw7w=",
    "Y3Vua3U=",
    "YsO2eWxl",
    "Ym95bGU=",
    "dMO8cmvDp2U=",
    "dHVya2Nl",
    "Z8O8bmNlbGxlbWU=",
    "aGF0YQ==",
    "ZG9zeWE=",
    "c2F0xLFy",
    "c2F0aXI=",
    "Zm9ua3NpeW9u",
    "ZGXEn2nFn2tlbg==",
    "ZGVnaXNrZW4=",
    "ZMO2bmTDvHI=",
    "ZG9uZHVy",
    "ZG/En3J1",
    "ZG9ncnU=",
    "eWFubMSxxZ8=",
    "eWFubGlz",
    "YcOnxLFrbGFtYQ==",
    "YWNpa2xhbWE=",
    "YmHFn2zEsWs=",
    "YmFzbGlr",
]

COMMON_TURKISH_WORDS = [base64.b64decode(b).decode("utf-8") for b in _B64_TURKISH_WORDS]

TURKISH_WORD_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in COMMON_TURKISH_WORDS) + r")\b",
    re.IGNORECASE,
)

# Proper name allowlist: The owner's surname ('Yücelen' / 'Yucelen') is an allowed
# proper noun, not a language violation.
ALLOWED_PROPER_NAMES = ["Y\u00fccelen", "Yucelen"]

# Control sample used to verify the detector fires (encoded so it does not trigger self-scan)
_ENCODED_CONTROL_SAMPLE = (
    b"QnUgZm9ua3NpeW9uIHNhZGVjZSB0w7xya8OnZSBrYXJha3RlcmxlciBpw6dlcmlyIHZlIGtvbnRyb2wgZWRpbGlyLg=="
)
TURKISH_CONTROL_SAMPLE = base64.b64decode(_ENCODED_CONTROL_SAMPLE).decode("utf-8")


def is_visitor_facing_page(rel_path: str) -> bool:
    """True for the public website's pages, which carry product copy, not code.

    `CLAUDE.md` requires English for code, comments, variable names, commit
    messages, documentation, tool descriptions and test names. Website copy is
    none of those: it is the text a visitor reads, and the launch design
    (`docs/superpowers/specs/2026-09-04-launch-website-design.md`) has the site
    served in Turkish to a Turkish reader. Decision 53 records the exemption.

    It is deliberately narrow. Only `.html` pages under `site/` are exempt, and
    only because those files hold the sentences a visitor reads. `site/*.js`
    and `site/*.css` are code and stay English, comments included, so any copy
    the scripts need must live in the page's markup rather than in a string
    inside the script.
    """
    normalized = rel_path.replace("\\", "/")
    return normalized.startswith("site/") and normalized.endswith(".html")


def find_turkish_violations(text: str, rel_path: str = "") -> list[tuple[int, str, str]]:
    """Scan text for Turkish characters or words, returning (line_no, reason, line)."""
    if is_visitor_facing_page(rel_path):
        return []

    violations: list[tuple[int, str, str]] = []
    lines = text.splitlines()

    for idx, line in enumerate(lines, start=1):
        # Exclude only specific, known citation line in goal 028.
        # This keeps the detector from flagging the goal file's character definition list
        # while catching any Turkish sentences added to either file.
        normalized_path = rel_path.replace("\\", "/")
        if (
            normalized_path == "docs/goals/028-english-only-guard.md"
            and "(`\u011f \u0131 \u015f \u011e \u0130 \u015e`)" in line
        ):
            continue

        # 1. Check for Turkish-specific characters
        found_chars = sorted({c for c in line if c in TURKISH_SPECIFIC_CHARS})
        if found_chars:
            violations.append(
                (
                    idx,
                    f"Contains Turkish-specific character(s): {', '.join(found_chars)}",
                    line.strip(),
                )
            )
            continue

        # 2. Check for common Turkish words after removing allowed proper names
        sanitized = line
        for name in ALLOWED_PROPER_NAMES:
            sanitized = re.sub(re.escape(name), "", sanitized, flags=re.IGNORECASE)

        match = TURKISH_WORD_PATTERN.search(sanitized)
        if match:
            violations.append(
                (
                    idx,
                    f"Contains Turkish word: '{match.group(0)}'",
                    line.strip(),
                )
            )

    return violations


def test_detector_fires_on_control_sample() -> None:
    """Break caught: the scanner breaks silently and tests pass vacuously."""
    violations = find_turkish_violations(TURKISH_CONTROL_SAMPLE)
    assert len(violations) > 0, "Detector failed to detect Turkish control string"


def test_owner_surname_is_allowed_narrowly() -> None:
    """Break caught: owner surname in LICENSE or docs fails, or an allowlist allows Turkish."""
    valid_name_line = "Copyright (c) 2026 Egemen Y\u00fccelen"
    assert find_turkish_violations(valid_name_line) == []

    # A Turkish sentence containing the name must still be flagged for other Turkish content.
    turkish_sentence_with_name = base64.b64decode(
        b"RWdlbWVuIFnDvGNlbGVuIGJ1IGZvbmtzaXlvbnUgZ2VsacWfdGlyZGkgdmUgdGVzdCBldHRpLg=="
    ).decode("utf-8")
    violations = find_turkish_violations(turkish_sentence_with_name)
    assert len(violations) > 0


def test_typographic_punctuation_and_box_drawing_are_not_flagged() -> None:
    """Break caught: Unicode arrows, dashes, or box-drawing chars trigger false positives."""
    sample = "Title \u2014 subtitle \u2013 info \u2192 next \u2502 \u250c \u2514 \u2022 \u2026"
    assert find_turkish_violations(sample) == []


def test_detector_would_fire_on_turkish_in_self_or_goal_file() -> None:
    """Break caught: self-exclusion is too broad (e.g. skips the whole file)."""
    fake_turkish_line = base64.b64decode(b"YnUgc2F0xLFyIHTDvHJrxYdlZGly").decode("utf-8")
    violations_self = find_turkish_violations(
        fake_turkish_line, rel_path="tests/test_english_only.py"
    )
    assert len(violations_self) > 0

    violations_goal = find_turkish_violations(
        fake_turkish_line, rel_path="docs/goals/028-english-only-guard.md"
    )
    assert len(violations_goal) > 0


def test_the_website_exemption_covers_pages_and_nothing_else() -> None:
    """Break caught: the site exemption widens until the English-only rule is gone.

    Turkish copy is allowed in the pages a visitor reads and nowhere else. The
    scripts and stylesheets that render those pages are code, so a Turkish
    string placed in one of them - the easy accident, since a script is where a
    developer reaches for a sentence - must still fail.
    """
    turkish_line = base64.b64decode(b"YnUgc2F0xLFyIHTDvHJrxYdlZGly").decode("utf-8")

    assert find_turkish_violations(turkish_line, rel_path="site/index.html") == []
    assert find_turkish_violations(turkish_line, rel_path="site/tr/index.html") == []

    for code_path in ("site/hero.js", "site/style.css", "site/sw.js", "docs/LAUNCH_COPY.md"):
        assert find_turkish_violations(turkish_line, rel_path=code_path), (
            f"{code_path} is code, not visitor-facing copy, and must stay English"
        )


def test_all_tracked_files_are_english_only() -> None:
    """Scan every tracked file in git for Turkish characters and words."""
    try:
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        tracked_files = [f.strip() for f in result.stdout.splitlines() if f.strip()]
    except FileNotFoundError, subprocess.CalledProcessError:
        excluded_parts = {".git", ".venv", ".tmp", ".pytest_cache", ".ruff_cache", "__pycache__"}
        tracked_files = []
        for path in REPO_ROOT.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(REPO_ROOT)
            if any(part in excluded_parts for part in relative.parts):
                continue
            tracked_files.append(relative.as_posix())

    assert len(tracked_files) > 50, "repository scan returned unexpectedly few files"

    all_violations: list[str] = []
    for rel_path in tracked_files:
        full_path = REPO_ROOT / rel_path
        if not full_path.is_file():
            continue

        try:
            content = full_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            # Binary file
            continue

        violations = find_turkish_violations(content, rel_path=rel_path)
        for line_no, reason, line in violations:
            all_violations.append(f"{rel_path}:{line_no}: {reason}\n    {line}")

    assert not all_violations, (
        f"Found {len(all_violations)} English-only violation(s):\n" + "\n".join(all_violations)
    )
