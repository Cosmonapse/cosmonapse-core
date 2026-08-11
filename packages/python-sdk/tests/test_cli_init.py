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

from pathlib import Path

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
