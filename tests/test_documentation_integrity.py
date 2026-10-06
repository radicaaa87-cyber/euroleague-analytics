"""Two facts about the documentation that a machine can check, and nothing else.

WHY ONLY TWO. No script can write down why a decision was taken, and a generated
document nobody reads is worse than a missing one. What a machine *can* do is
notice when the written record and the code have drifted apart. These are the two
places that actually drifted on 2026-08-30:

  * `MCP_REQUIRED_SCOPE` was added to the code and not to `.env.example`. An
    operator reading the example file would never have known the setting exists.
  * `DECISIONS.md` is referenced by number from code comments, tests and other
    documents. A reference to a decision that does not exist is a dead end, and
    nothing was checking.

WHAT THESE DO NOT DO. They say nothing about whether the documentation is
correct, current, or worth reading. A `.env.example` entry can describe the wrong
thing and pass; a decision can be obsolete and still resolve. These catch
absence, which is the failure that actually happens.
"""

from __future__ import annotations

import re
from pathlib import Path

SOURCE_DIRECTORIES = (Path("src"), Path("scripts"))

ENVIRONMENT_READ = re.compile(
    r"""(?:os\.environ\.get\(|os\.getenv\(|os\.environ\[|values\.get\(|values\[|env\.get\(|environment\.get\()["']([A-Z][A-Z0-9_]{2,})["']"""
)

# Variables that are configuration for somebody else, not for this project's
# operator. Each is listed with the reason it is not in `.env.example`.
NOT_OUR_CONFIGURATION = {
    "GITHUB_OUTPUT",  # written by the Actions runner, never set by a person
    "GITHUB_STEP_SUMMARY",  # same
    "GITHUB_ACTIONS",  # set by the runner to announce itself
    "CI",  # conventional, set by every CI system
    "PATH",  # the operating system's
    "PORT",  # supplied by Fly at runtime, not configured by hand
    "HOST",  # same: the address the container binds, given by the platform
    "GITHUB_RUN_ID",  # written by the Actions runner
    "GITHUB_SHA",  # written by the Actions runner for the checked-out commit
    "RUNNER_OS",  # same
    "EL_MCP_TOKEN",  # deliberately absent: a credential that must never be
    # written to a file, including an example one. See
    # scripts/check_hosted_token.py.
    "TZ",
    "PYTHONPATH",
}

DECISION_REFERENCE = re.compile(r"[Dd]ecisions?\s+(?:item\s+)?(\d{1,3})\b")
DECISION_HEADING = re.compile(r"^## (\d{1,3})\.", re.MULTILINE)
LAUNCH_REPOSITORY_URL = "https://github.com/egemeny13/euroleague-analytics-launch"


def _python_files() -> list[Path]:
    files: list[Path] = []
    for directory in SOURCE_DIRECTORIES:
        files.extend(sorted(directory.rglob("*.py")))
    assert files, "No Python sources found; the search roots are wrong."
    return files


def test_every_environment_variable_the_code_reads_is_in_the_example_file() -> None:
    """An operator configures this project by copying `.env.example`.

    A variable the code reads and the example omits is a setting that exists and
    cannot be discovered. `MCP_REQUIRED_SCOPE` was exactly that for part of
    2026-08-30, and it decides whether the hosted server accepts a token.
    """
    example = Path(".env.example").read_text(encoding="utf-8")
    documented = {
        line.split("=", 1)[0].strip()
        for line in example.splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }

    undocumented: dict[str, str] = {}
    for path in _python_files():
        for name in ENVIRONMENT_READ.findall(path.read_text(encoding="utf-8")):
            if name in NOT_OUR_CONFIGURATION or name in documented:
                continue
            undocumented.setdefault(name, path.as_posix())

    assert not undocumented, (
        "These environment variables are read by the code and absent from "
        ".env.example, so nobody configuring this project can discover them:\n  "
        + "\n  ".join(f"{name}  (read in {where})" for name, where in sorted(undocumented.items()))
        + "\nAdd them with a line saying what they do, or add them to "
        "NOT_OUR_CONFIGURATION with the reason they do not belong there."
    )


def test_every_decision_referenced_by_number_exists() -> None:
    """`DECISIONS.md` is cited by number from code, tests and other documents.

    A citation that resolves to nothing is worse than no citation: it reads as
    evidence that a choice was reasoned through somewhere else.
    """
    decisions_text = Path("DECISIONS.md").read_text(encoding="utf-8")
    existing = {int(number) for number in DECISION_HEADING.findall(decisions_text)}
    assert existing, "No numbered decisions found in DECISIONS.md; the heading shape changed."

    sources = [*_python_files(), *sorted(Path("tests").glob("*.py"))]
    sources += [Path("CLAUDE.md"), Path("ROADMAP.md")]
    sources += sorted(Path("docs").glob("*.md"))

    dangling: dict[str, set[int]] = {}
    for path in sources:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if path.name == "DECISIONS.md":
            continue
        for number in {int(found) for found in DECISION_REFERENCE.findall(text)}:
            if number not in existing:
                dangling.setdefault(path.as_posix(), set()).add(number)

    assert not dangling, (
        "These files cite a decision number that DECISIONS.md does not define:\n  "
        + "\n  ".join(f"{where}: {sorted(numbers)}" for where, numbers in sorted(dangling.items()))
        + f"\nDECISIONS.md currently defines 1 to {max(existing)}."
    )


def test_launch_asset_repository_ownership_is_documented() -> None:
    """Media production must not silently drift back into this warehouse repo.

    The launch package was split into its own repository on 2026-09-01, but the
    owner checklist still pointed agents at the superseded worktree the next
    day. This check cannot prove the remote repository is available or current.
    It only makes the ownership boundary discoverable from every local planning
    document that an agent is expected to follow.
    """
    documents = {
        "DECISIONS.md": Path("DECISIONS.md").read_text(encoding="utf-8"),
        "ROADMAP.md": Path("ROADMAP.md").read_text(encoding="utf-8"),
        "docs/OWNER_LAUNCH_STEPS.md": Path("docs/OWNER_LAUNCH_STEPS.md").read_text(
            encoding="utf-8"
        ),
        "docs/LAUNCH_THREAD_FINAL.md": Path("docs/LAUNCH_THREAD_FINAL.md").read_text(
            encoding="utf-8"
        ),
    }

    missing = [name for name, text in documents.items() if LAUNCH_REPOSITORY_URL not in text]
    assert not missing, (
        "The separate launch repository is absent from these planning documents: "
        + ", ".join(missing)
    )

    owner_steps = documents["docs/OWNER_LAUNCH_STEPS.md"]
    assert "create_launch_video_remotion" not in owner_steps, (
        "The owner checklist still routes launch work to the superseded worktree."
    )
    assert "`site/`" in owner_steps, (
        "The ownership note must also say that the deployable website stays in this repo."
    )


def test_every_migration_has_a_matching_down() -> None:
    """`scripts/migration_gate.py` needs the pair, and it needs a database to say so.

    The gate applies every `up`, reverses it with the `down`, and applies it
    again. A migration written without its `down` therefore breaks the gate
    rather than this suite, and the gate only runs against a disposable
    PostgreSQL that nobody has to hand. That pushes the discovery to whoever
    next tries to rehearse a release, which is the worst moment to find it.
    """
    migrations = Path("migrations")
    missing = [
        f"{path.name.removesuffix('.up.sql')}.down.sql"
        for path in sorted(migrations.glob("*.up.sql"))
        if not (migrations / f"{path.name.removesuffix('.up.sql')}.down.sql").exists()
    ]
    assert not missing, (
        "These migrations have no rollback, so the migration gate cannot run:\n  "
        + "\n  ".join(missing)
    )


def test_every_migration_is_recorded_in_the_ledger() -> None:
    """A migration absent from `migrations/README.md` is a schema change with no reason.

    The table in that file is the ledger: what each migration creates, when it
    was applied, and what it was approved by. This checks only that a row
    exists - it cannot check that the row is true, and a row saying nothing
    useful passes.
    """
    ledger = (Path("migrations") / "README.md").read_text(encoding="utf-8")
    recorded = set(re.findall(r"^\|\s*`([0-9][0-9a-z_]*)`", ledger, re.MULTILINE))

    unrecorded = sorted(
        path.name.removesuffix(".up.sql")
        for path in Path("migrations").glob("*.up.sql")
        if path.name.removesuffix(".up.sql") not in recorded
    )
    assert not unrecorded, (
        "These migrations change the schema and appear nowhere in the ledger at "
        "migrations/README.md:\n  " + "\n  ".join(unrecorded)
    )
