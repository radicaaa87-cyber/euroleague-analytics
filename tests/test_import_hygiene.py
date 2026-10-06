"""The import mistake that passes locally and fails only in CI.

`tests/conftest.py` has warned about this since Phase 2, and the repository has
now been caught by it twice - most recently on 2026-08-19, when
`tests/test_incremental_confirmation.py` imported from `scripts.` and CI failed
on the first pull request that could run it.

WHY IT HIDES. `python -m pytest` puts the working directory on `sys.path`, so a
`scripts.` import resolves. The bare `pytest` that CI runs does not. Anyone
developing with `python -m pytest` therefore sees green right up until the
change reaches CI - and until 2026-08-19 no CI run had ever been triggered on a
branch, so 23 commits accumulated behind it.

`scripts/` is a directory of entry points, not an installed package. Anything a
test needs belongs in `src/euroleague/`, which `pip install -e .` puts on the
path from any working directory.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

TESTS_ROOT = Path(__file__).resolve().parent
TEST_MODULES = sorted(TESTS_ROOT.glob("test_*.py"))


def _imported_roots(path: Path) -> set[str]:
    """Top-level package name of every import in one module."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_there_are_test_modules_to_check() -> None:
    """Break caught: the glob stops matching and every check below passes vacuously."""
    assert len(TEST_MODULES) > 20


@pytest.mark.parametrize("module", TEST_MODULES, ids=lambda path: path.name)
def test_no_test_module_imports_from_the_scripts_directory(module: Path) -> None:
    """Break caught: an import that resolves under `python -m pytest` and not under `pytest`."""
    assert "scripts" not in _imported_roots(module), (
        f"{module.name} imports from `scripts`, which is not an installed package. "
        f"It will pass under `python -m pytest` and fail in CI under bare `pytest`. "
        f"Move the shared code into `src/euroleague/` and import it from there."
    )


@pytest.mark.parametrize("module", TEST_MODULES, ids=lambda path: path.name)
def test_no_test_module_imports_another_test_module(module: Path) -> None:
    """Break caught: shared helpers move into a test module instead of conftest.

    Same failure shape and the same cause. `tests/conftest.py` says exactly why
    the fake connection lives there rather than in a test module, and this is
    the check that keeps it true.
    """
    offenders = {root for root in _imported_roots(module) if root.startswith("test_")}
    assert not offenders, (
        f"{module.name} imports test module(s) {sorted(offenders)}. Put shared fixtures "
        f"in tests/conftest.py, which pytest injects without an import."
    )


# --------------------------------------------------------------------------
# The second way a test passes here and fails in CI: an undeclared dependency.
#
# On 2026-09-02 a new test did `import yaml` and went green locally. PyYAML is
# installed in this machine's virtualenv as a side effect of the agent factory
# tooling under `.agents/`, and appears in no requirements file. CI installs
# `requirements-dev.txt` and nothing else, so it failed with ModuleNotFoundError
# on the first push - which is the only place the mistake was visible.
#
# The check below closes that gap: every third-party package imported by `src/`
# or `tests/` must be listed in IMPORT_ROOT_TO_REQUIREMENT, and every
# requirements file named there must really declare it. Adding an undeclared
# dependency now fails locally, before the push.
#
# WHAT IT DOES NOT CATCH, stated rather than omitted:
#   - Version drift. It checks that a package is declared, not that the pinned
#     version matches the installed one.
#   - Transitive imports. If `src/` imports `mcp` and `mcp` imports something
#     undeclared, that is the pinned tree's problem, not this check's.
#   - `scripts/`. Those entry points are not imported by the test suite (see
#     the check above) and are not run by CI, so their imports are unchecked.
#   - A package declared in a requirements file that nothing imports. Removing
#     a dead dependency is not this test's job.

REPO_ROOT = TESTS_ROOT.parent
SOURCE_MODULES = sorted((REPO_ROOT / "src").rglob("*.py"))

# Import root -> the requirements file that must declare it. The keys are what
# `import X` writes; the values are checked as substrings of that file, so
# `PyYAML` matches the `PyYAML==6.0.3` line whatever the pin says.
IMPORT_ROOT_TO_REQUIREMENT: dict[str, tuple[str, str]] = {
    "requests": ("requirements.txt", "requests"),
    "psycopg": ("requirements.txt", "psycopg"),
    "numpy": ("requirements-dev.txt", "numpy"),
    "pytest": ("requirements-dev.txt", "pytest"),
    "mcp": ("requirements-http.txt", "mcp"),
    "pydantic": ("requirements-http.txt", "pydantic"),
    "starlette": ("requirements-http.txt", "starlette"),
    "uvicorn": ("requirements-http.txt", "uvicorn"),
    "httpx2": ("requirements-http.txt", "httpx2"),
    "jwt": ("requirements-http.txt", "pyjwt"),
    "anyio": ("requirements-http.txt", "anyio"),
}

# First-party and non-package roots that need no declaration.
LOCAL_ROOTS = {"euroleague", "conftest", "tests", "scripts"}


def _third_party_roots(path: Path) -> set[str]:
    """Import roots that are neither standard library nor part of this repository."""
    roots = _imported_roots(path)
    return {
        root
        for root in roots
        if root not in sys.stdlib_module_names
        and root not in LOCAL_ROOTS
        and not root.startswith("test_")
        and not root.startswith("_")
    }


def test_there_are_source_modules_to_check() -> None:
    """Break caught: the src glob stops matching and the dependency check passes vacuously."""
    assert len(SOURCE_MODULES) > 5


@pytest.mark.parametrize(
    "module",
    TEST_MODULES + SOURCE_MODULES,
    ids=lambda path: path.name,
)
def test_every_third_party_import_is_a_declared_dependency(module: Path) -> None:
    """Break caught: an import that resolves in a local virtualenv and not in CI."""
    undeclared = sorted(_third_party_roots(module) - IMPORT_ROOT_TO_REQUIREMENT.keys())
    assert not undeclared, (
        f"{module.name} imports {undeclared}, which no requirements file declares. "
        f"CI installs requirements-dev.txt and nothing else, so this passes here and "
        f"fails there with ModuleNotFoundError. Either add the package to a "
        f"requirements file and to IMPORT_ROOT_TO_REQUIREMENT in this test, or drop "
        f"the import - the standard library is usually enough."
    )


@pytest.mark.parametrize("import_root", sorted(IMPORT_ROOT_TO_REQUIREMENT))
def test_each_declared_dependency_really_appears_in_its_requirements_file(
    import_root: str,
) -> None:
    """Break caught: the map above drifts from the requirements files it claims to mirror."""
    requirements_file, distribution = IMPORT_ROOT_TO_REQUIREMENT[import_root]
    text = (REPO_ROOT / requirements_file).read_text(encoding="utf-8")
    declared = [
        line
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "-"))
    ]
    assert any(distribution.lower() in line.lower() for line in declared), (
        f"`import {import_root}` is mapped to {distribution!r} in {requirements_file}, "
        f"but that file does not declare it. The map and the requirements files have "
        f"drifted; fix whichever one is wrong."
    )
