"""`cosmo init` end to end, through Click.

This file exists because of a specific escape: the command's body had been
refactored into `scaffold_project()` and the *old* inline body was left
underneath it, so `cosmo init NAME` wrote the whole skeleton and then failed
the "directory already has files" precondition against the files it had just
written. Fresh directory, complete scaffold on disk, exit code 1.

Every gate missed it. ruff was clean (the dead `written` binding is RUF059,
which this project ignores), mypy was clean, and 494 tests passed - none of
which invoked the CLI command. The unit-level `scaffold_project` was fine the
whole time; only the Click wrapper was broken. So these tests drive the actual
command through CliRunner rather than calling the helper.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from cosmo.commands.init import init


def test_init_into_empty_dir_succeeds(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(init, ["demo"])
        assert result.exit_code == 0, result.output
        assert "Scaffolded demo" in result.output
        assert Path("demo/brain.py").is_file()
        assert Path("demo/config.py").is_file()
        assert Path("demo/neurons/hello.py").is_file()
        assert Path("demo/receptors/terminal.py").is_file()


def test_init_lists_every_file_it_wrote(tmp_path: Path) -> None:
    """The `+ file` lines must match what is on disk, not a second inventory."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(init, ["demo"])
        assert result.exit_code == 0, result.output
        listed = [
            line.strip()[2:] for line in result.output.splitlines()
            if line.strip().startswith("+ ")
        ]
        assert listed
        for rel in listed:
            assert Path("demo", rel).is_file(), f"reported {rel}, not on disk"


def test_init_refuses_a_populated_dir_without_force(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        assert runner.invoke(init, ["demo"]).exit_code == 0
        result = runner.invoke(init, ["demo"])
        assert result.exit_code != 0
        assert "already contains" in result.output
        # The CLI must name the flag a terminal user can actually type; the
        # underlying helper's wording is transport-neutral for Genesis.
        assert "--force" in result.output


def test_init_force_overwrites(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        assert runner.invoke(init, ["demo"]).exit_code == 0
        Path("demo/brain.py").write_text("# clobbered\n", encoding="utf-8")
        result = runner.invoke(init, ["demo", "--force"])
        assert result.exit_code == 0, result.output
        assert "# clobbered" not in Path("demo/brain.py").read_text(encoding="utf-8")


def test_init_namespace_reaches_the_scaffold(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(init, ["demo", "--namespace", "prod-ns"])
        assert result.exit_code == 0, result.output
        assert "prod-ns" in Path("demo/config.py").read_text(encoding="utf-8")


# ── the repository the scaffold now starts ────────────────────────────────
# `cosmo init` writes a .gitignore unconditionally and starts a repository
# unless told not to. Both are additions to a command whose output format one
# of the tests above depends on, so they get covered here rather than in
# test_genesis_git.py.

def test_init_writes_a_gitignore(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        assert runner.invoke(init, ["demo", "--no-git"]).exit_code == 0
        ignored = Path("demo/.gitignore").read_text(encoding="utf-8")
        assert ".env" in ignored
        assert "_archive/" in ignored


def test_init_does_not_clobber_an_existing_gitignore(tmp_path: Path) -> None:
    """An existing .gitignore is somebody's decision. Replacing it silently is
    how a project starts committing its own .env."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        Path("demo").mkdir()
        Path("demo/.gitignore").write_text("# mine\n", encoding="utf-8")
        result = runner.invoke(init, ["demo", "--no-git"])
        assert result.exit_code == 0, result.output
        assert Path("demo/.gitignore").read_text(encoding="utf-8") == "# mine\n"


def test_init_no_git_leaves_no_repository(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(init, ["demo", "--no-git"])
        assert result.exit_code == 0, result.output
        assert not Path("demo/.git").exists()
        assert "git:" not in result.output


@pytest.mark.skipif(shutil.which("git") is None, reason="needs the git binary")
def test_init_starts_a_repository_by_default(tmp_path: Path, monkeypatch) -> None:
    empty = tmp_path / "empty-gitconfig"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(empty))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Ada Lovelace")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "ada@example.com")

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(init, ["demo"])
        assert result.exit_code == 0, result.output
        assert Path("demo/.git").is_dir()
        assert "git:" in result.output


@pytest.mark.skipif(shutil.which("git") is None, reason="needs the git binary")
def test_init_inside_an_existing_repo_still_scaffolds(tmp_path: Path) -> None:
    """The repository is the optional half. A folder that is already inside
    one gets the scaffold and a note, never a failed init."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as cwd:
        subprocess.run(["git", "init", "-q"], cwd=cwd, check=True)  # noqa: S607
        result = runner.invoke(init, ["demo"])
        assert result.exit_code == 0, result.output
        assert Path("demo/brain.py").is_file()
        assert not Path("demo/.git").exists()
        assert "already inside the git repository" in result.output
