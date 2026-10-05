"""The manual archive restore gate and the rails around its production access.

These tests read the workflow as text, matching the repository's existing
workflow tests and avoiding a YAML dependency. They pin the properties that can
be proved before the workflow has ever run: a person supplies one season, the
season is shell data rather than shell source, only the verifier receives
production credentials, and the run cannot overlap the archive fetcher.

WHAT THESE TESTS DO NOT PROVE. Static text cannot prove that GitHub accepts and
executes the workflow, that the configured secrets are correct, or that the
production archive can be reached. The workflow remains operationally unproven
until somebody deliberately dispatches it and observes a restore gate pass.
"""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOW = Path(".github/workflows/verify-archive-season.yml")
CI_WORKFLOW = Path(".github/workflows/ci.yml")

PRODUCTION_SECRETS = (
    "DATABASE_URL",
    "SUPABASE_URL",
    "SUPABASE_SERVICE_ROLE_KEY",
)


def _text() -> str:
    """Read the workflow; a missing file is itself the clearest test failure."""
    return WORKFLOW.read_text(encoding="utf-8")


def _pinned_in_ci(action: str) -> str:
    """Return the `action@sha` reference that ci.yml pins for `action`.

    WHY THIS READS CI RATHER THAN REPEATING A LITERAL. What matters here is that
    the restore gate uses the same pinned action as the rest of the repository,
    not which commit that happens to be this month. A literal copied into this
    file asserts the second thing while appearing to assert the first, and it
    fails whenever a dependency update moves every workflow together correctly -
    which is exactly what the actions/checkout v5 to v7 bump did on 2026-08-30.

    The pattern still demands a 40 character commit SHA, so ci.yml sliding back
    to a mutable tag such as `@v7` fails here rather than quietly lowering the
    bar for both files at once.

    WHAT THIS DOES NOT PROVE. It compares the `owner/action@sha` reference only,
    not the trailing `# v7.0.1` comment. A workflow carrying the right SHA under
    a misleading version comment passes; the SHA is the mechanism and the comment
    is a label. It also says nothing about what the action's code does.
    """
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    match = re.search(rf"uses: ({re.escape(action)}@[0-9a-f]{{40}})", text)
    assert match, f"ci.yml does not pin {action} to a commit SHA."
    return match.group(1)


def _step(text: str, name: str) -> str:
    """Return one named step's text without claiming to parse arbitrary YAML."""
    marker = f"      - name: {name}"
    assert marker in text, f"Missing workflow step: {name}"
    remainder = text.split(marker, 1)[1]
    return remainder.split("\n      - ", 1)[0]


def test_the_restore_gate_is_manual_and_requires_a_season() -> None:
    """Catch automatic or defaulted runs; not malformed season values at runtime."""
    text = _text()
    triggers = text.split("permissions:", 1)[0]

    assert "workflow_dispatch:" in triggers
    assert re.search(r"^\s{6}season:\s*$", triggers, re.MULTILINE)
    assert "required: true" in triggers
    assert not re.search(r"^\s+default:", triggers, re.MULTILINE)
    assert not re.search(r"^\s{2}(schedule|push):", triggers, re.MULTILINE)


def test_the_restore_gate_uses_the_hardened_action_setup() -> None:
    """Catch weaker permissions or mutable actions; not unsafe code inside an action."""
    text = _text()

    assert re.search(r"^permissions:\s*\n\s{2}contents: read$", text, re.MULTILINE)
    assert f"uses: {_pinned_in_ci('actions/checkout')}" in text
    assert "persist-credentials: false" in text
    assert re.search(r"uses: actions/setup-python@[0-9a-f]{40}", text)


def test_only_the_verifier_receives_production_credentials() -> None:
    """Catch job-wide or install-step secrets; not a malicious installed package later."""
    text = _text()
    job_before_steps = text.split("    steps:", 1)[0].split("jobs:", 1)[1]
    install_step = _step(text, "Install dependencies")
    verify_step = _step(text, "Verify the archived season restores byte for byte")

    assert "env:" not in job_before_steps
    assert "secrets." not in install_step
    for secret in PRODUCTION_SECRETS:
        binding = f"{secret}: ${{{{ secrets.{secret} }}}}"
        assert binding in verify_step
        assert text.count(binding) == 1


def test_the_season_reaches_the_verifier_as_shell_data() -> None:
    """Catch expression interpolation in run source; not bugs inside validation code."""
    text = _text()
    verify_step = _step(text, "Verify the archived season restores byte for byte")

    assert "SEASON: ${{ inputs.season }}" in verify_step
    assert 'run: python scripts/verify_archive_season.py "$SEASON"' in verify_step
    run_line = next(line for line in verify_step.splitlines() if line.strip().startswith("run:"))
    assert "${{" not in run_line


def test_the_restore_gate_cannot_compete_with_archive_fetching() -> None:
    """Catch a missing shared queue; not GitHub service failures in concurrency."""
    text = _text()

    assert re.search(
        r"concurrency:\s*\n(?:\s*#[^\n]*\n)*\s*group: e2026-live-fetcher\s*\n"
        r"\s*cancel-in-progress: false",
        text,
    )


def test_the_header_keeps_the_reason_for_the_manual_gate() -> None:
    """Catch deletion of the incident context; not whether the prose is persuasive."""
    header = _text().split("name:", 1)[0]

    assert "E2020" in header
    assert "E2021" in header
    assert "timeout" in header.lower()
    assert "fetched" in header.lower()
    assert "verified" in header.lower()
