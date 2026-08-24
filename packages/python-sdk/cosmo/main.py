"""
cosmo  -  Cosmonapse developer CLI

Commands
--------
cosmo init        Scaffold a minimal Axon + Dendrite project
cosmo synapse     Manage Synapse servers (start / view / stop)
cosmo dispatch    Dispatch a TASK from the terminal and await the reply
cosmo registry    Inspect the live Neuron population (DISCOVER-based)
cosmo answer      Interactively answer CLARIFICATION / PERMISSION requests
cosmo schema      Export the Signal envelope JSON Schema
cosmo prism       Open Prism, the live browser view onto a Synapse namespace
cosmo validate    Validate that Signals on the Synapse conform to the envelope spec
cosmo genesis     Open Genesis - name a brain, scaffold it, grow it on a canvas
cosmo completion  Print a shell-completion script (bash / zsh / fish)

Genesis and Prism are the two commands an edition swaps out: a cloud image
installs its own build of them in place of the ones bundled here. They are
therefore imported defensively - with either absent, `cosmo` still starts,
every other command still works, and the missing one fails as a plain
"No such command" rather than a traceback on every single invocation. This
module is the only place in the package that imports them.

Root help
---------
Click's stock help formatter renders one alphabetical command list - correct,
but flat. `cosmo` has a shape (scaffold once, run things, watch what's
happening, open a browser tool), and `_CosmoGroup` below renders that shape
with the same [bold cyan] lockup `cosmo prism` / `cosmo genesis` already print
at startup, so the root command reads like it belongs to the same CLI rather
than falling back to click's generic listing. See `cosmo/commands/_shared.py`
for the console/legacy-Windows handling this borrows (`_HAS_RICH`).
"""

import click

from cosmo.commands._shared import _HAS_RICH
from cosmo.commands.answer import answer
from cosmo.commands.completion import completion
from cosmo.commands.dispatch import dispatch
from cosmo.commands.init import init
from cosmo.commands.registry import registry
from cosmo.commands.schema import schema
from cosmo.commands.synapse import synapse
from cosmo.commands.validate import validate

try:
    # doppler is a deprecated alias for `cosmo prism`; hidden from --help.
    from cosmo.commands.prism import doppler, prism
except ImportError:
    prism = doppler = None

try:
    from cosmo.commands.genesis import genesis
except ImportError:
    genesis = None


if _HAS_RICH:
    from rich.console import Console

    # Same legacy_windows reasoning as `cosmo/commands/_shared.py`: a
    # redirected stdout fails GetConsoleMode, and letting rich guess wrong
    # about the console kind is how a banner write turns into a crash instead
    # of ordinary output.
    def _stdout_is_console() -> bool:
        try:
            import sys
            return bool(sys.stdout is not None and sys.stdout.isatty())
        except Exception:
            return False

    _console = Console() if _stdout_is_console() else Console(legacy_windows=False)


# Root help, grouped by what you'd reach for: scaffold once, run things,
# watch what's happening, open a browser tool. Filtered against whatever
# commands are actually registered, so an edition without genesis/prism (or
# a future command not listed here) degrades gracefully - present commands
# are grouped, anything unlisted falls into "more".
_COMMAND_GROUPS: list[tuple[str, list[str]]] = [
    ("build", ["init", "genesis"]),
    ("run", ["synapse", "dispatch", "answer"]),
    ("observe", ["registry", "validate", "schema", "prism"]),
    ("shell", ["completion"]),
]


class _CosmoGroup(click.Group):
    """The `cosmo` root group: click's command tree, rendered as a banner +
    grouped command table instead of click's default flat listing."""

    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        if not _HAS_RICH:
            super().format_help(ctx, formatter)
            return

        commands = self.commands
        grouped = {name for _, names in _COMMAND_GROUPS for name in names}
        leftover = [n for n in sorted(commands) if n not in grouped]

        _console.print()
        _console.print("  [bold cyan]cosmo[/bold cyan]  [dim]-  the Cosmonapse developer CLI[/dim]")
        _console.print()
        _console.print("  [dim]Usage:[/dim]  cosmo [cyan]COMMAND[/cyan] [dim][OPTIONS][/dim]")
        _console.print()

        for label, names in [*_COMMAND_GROUPS, ("more", leftover)]:
            present = [(n, commands[n]) for n in names if n in commands]
            if not present:
                continue
            _console.print(f"  [bold]{label}[/bold]")
            for name, cmd in present:
                try:
                    summary = cmd.get_short_help_str(limit=72)
                except Exception:
                    summary = ""
                _console.print(f"    [cyan]{name:<11}[/cyan] [dim]{summary}[/dim]")
            _console.print()

        _console.print("  [dim]Quick start:[/dim]  cosmo init my-brain  &&  cosmo synapse start  &&  cosmo genesis")
        _console.print()
        _console.print("  Run [bold]cosmo COMMAND --help[/bold] for details on any command.", style="dim")
        _console.print()


@click.group(cls=_CosmoGroup)
@click.version_option(package_name="cosmonapse", prog_name="cosmo")
def cli() -> None:
    """Cosmonapse developer tooling."""


cli.add_command(init)
cli.add_command(synapse)
cli.add_command(dispatch)
cli.add_command(registry)
cli.add_command(answer)
cli.add_command(schema)
cli.add_command(validate)
cli.add_command(completion)

# Registered only when whatever wheel provides them is installed.
for _command in (prism, doppler, genesis):
    if _command is not None:
        cli.add_command(_command)


if __name__ == "__main__":
    cli()
