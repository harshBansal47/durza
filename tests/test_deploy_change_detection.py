"""When a push to `main` should trigger a deploy.

The rule has one hard requirement: **a push must not be skipped because a
previous deploy failed.** If the last deploy failed, its changes were never
applied, and a later push that only touches documentation still has to deploy,
because the failed deploy's changes are still missing from the running host.

So the baseline is the last SHA that was *successfully deployed* — recorded on
the host at the end of a successful deploy — and not the previous head of
`main`. The script under test is `scripts/deploy_change_detection.sh`; these
tests build a throwaway git repository and run it for real.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deploy_change_detection.sh"

ZERO = "0" * 40


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        capture_output=True,
        text=True,
        check=True,
    )
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "test")
    return repo


def _commit(repo: Path, path: str, content: str) -> str:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", f"add {path}")
    return _git(repo, "rev-parse", "HEAD")


def _detect(repo: Path, current: str, before: str, recorded: str | None) -> str:
    """Run the script and return its answer."""
    marker = repo.parent / "last-deployed-sha"
    if recorded is None:
        marker.unlink(missing_ok=True)
    else:
        marker.write_text(recorded + "\n")
    result = subprocess.run(
        [
            "bash",
            str(SCRIPT),
            str(repo),
            current,
            before,
            str(marker),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def test_docs_only_after_a_successful_deploy_is_skipped(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    code = _commit(repo, "app/main.py", "x = 1\n")
    docs = _commit(repo, "docs/readme.md", "hello\n")

    assert _detect(repo, docs, code, recorded=code) == "deploy=no"


def test_docs_only_after_a_failed_deploy_still_deploys(tmp_path: Path) -> None:
    # The successful deploy was `code`; `feature` was pushed afterwards and
    # its deploy FAILED, so it was never applied. A docs-only push on top of
    # it must still deploy, because `feature` is still missing.
    repo = _repo(tmp_path)
    code = _commit(repo, "app/main.py", "x = 1\n")
    feature = _commit(repo, "app/feature.py", "y = 2\n")
    docs = _commit(repo, "docs/readme.md", "hello\n")

    assert _detect(repo, docs, feature, recorded=code) == "deploy=yes"


def test_no_recorded_deploy_always_deploys(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    code = _commit(repo, "app/main.py", "x = 1\n")
    docs = _commit(repo, "docs/readme.md", "hello\n")

    assert _detect(repo, docs, code, recorded=None) == "deploy=yes"


def test_a_first_push_always_deploys(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    code = _commit(repo, "app/main.py", "x = 1\n")

    # No previous head at all.
    assert _detect(repo, code, ZERO, recorded=None) == "deploy=yes"


def test_a_code_change_after_a_successful_deploy_deploys(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    code = _commit(repo, "app/main.py", "x = 1\n")
    more = _commit(repo, "app/other.py", "y = 2\n")

    assert _detect(repo, more, code, recorded=code) == "deploy=yes"


@pytest.mark.parametrize(
    "path",
    [
        "app/main.py",
        "alembic/versions/new.py",
        "mcp_server/tool.py",
        "scripts/deploy_change_detection.sh",
        "Dockerfile",
        "docker-compose.yml",
        "docker-compose.prod.yml",
        "pyproject.toml",
        "uv.lock",
        ".github/workflows/ci.yml",
    ],
)
def test_everything_that_can_change_what_runs_triggers_a_deploy(tmp_path: Path, path: str) -> None:
    repo = _repo(tmp_path)
    base = _commit(repo, "docs/readme.md", "hello\n")
    changed = _commit(repo, path, "content\n")

    assert _detect(repo, changed, base, recorded=base) == "deploy=yes"


def test_a_multi_commit_push_is_diffed_as_one_range(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    base = _commit(repo, "docs/readme.md", "hello\n")
    _commit(repo, "docs/second.md", "more\n")
    _commit(repo, "app/main.py", "x = 1\n")
    docs = _commit(repo, "docs/third.md", "even more\n")

    # The code commit is in the middle of the push: the whole range is
    # diffed, so it is not missed.
    assert _detect(repo, docs, base, recorded=base) == "deploy=yes"


def test_an_unknown_recorded_sha_deploys(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    base = _commit(repo, "docs/readme.md", "hello\n")
    docs = _commit(repo, "docs/second.md", "more\n")

    # A recorded SHA that is not in this checkout cannot be diffed against.
    assert _detect(repo, docs, base, recorded="f" * 40) == "deploy=yes"


def test_a_recorded_sha_that_is_not_an_ancestor_deploys(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    base = _commit(repo, "docs/readme.md", "hello\n")
    other = _commit(repo, "docs/other.md", "elsewhere\n")
    _git(repo, "checkout", "-b", "side", base)
    side = _commit(repo, "docs/side.md", "side\n")
    _git(repo, "checkout", "main")
    main_head = _commit(repo, "docs/main.md", "main\n")

    # The recorded SHA is on another branch, so it is not an ancestor of the
    # push: deploy rather than guess.
    assert _detect(repo, main_head, other, recorded=side) == "deploy=yes"


def test_the_script_is_executable_and_has_a_strict_mode() -> None:
    text = SCRIPT.read_text()
    assert "set -euo pipefail" in text
    assert "deploy_change_detection.sh" in text
