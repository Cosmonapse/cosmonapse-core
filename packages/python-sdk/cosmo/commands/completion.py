"""
cosmo completion
~~~~~~~~~~~~~~~~~
Print a shell-completion script for the ``cosmo`` CLI.

Completion is driven by Click's built-in machinery. This command renders the
script for a given shell so it can be sourced directly or written to the
shell's completion directory.

Examples
--------
    # bash  -  load for the current shell
    eval "$(cosmo completion bash)"

    # bash  -  install permanently
    cosmo completion bash > ~/.local/share/bash-completion/completions/cosmo

    # zsh  -  add to a directory on $fpath
    cosmo completion zsh > ~/.zfunc/_cosmo

    # fish
    cosmo completion fish > ~/.config/fish/completions/cosmo.fish
"""

from __future__ import annotations

import click
from click.shell_completion import get_completion_class

# Click derives the completion env var from the program name: COSMO → _COSMO_COMPLETE.
_COMPLETE_VAR = "_COSMO_COMPLETE"
_PROG_NAME = "cosmo"

_INSTALL_HINTS = {
    "bash": (
        "# Add to ~/.bashrc:\n"
        '#   eval "$(cosmo completion bash)"\n'
        "# Or install once:\n"
        "#   cosmo completion bash > "
        "~/.local/share/bash-completion/completions/cosmo"
    ),
    "zsh": (
        "# Add to ~/.zshrc:\n"
        '#   eval "$(cosmo completion zsh)"\n'
        "# Or write to a directory on $fpath:\n"
        "#   cosmo completion zsh > ~/.zfunc/_cosmo"
    ),
    "fish": (
        "# Install once:\n"
        "#   cosmo completion fish > ~/.config/fish/completions/cosmo.fish"
    ),
}


@click.command("completion")
@click.argument("shell", type=click.Choice(["bash", "zsh", "fish"]))
@click.option("--no-hint", is_flag=True, default=False,
              help="Print only the script, with no leading install-hint comment.")
def completion(shell: str, no_hint: bool) -> None:
    """Output a shell-completion script for SHELL (bash, zsh, or fish).

    \b
    Quick start:
      eval "$(cosmo completion bash)"     # current bash session
      eval "$(cosmo completion zsh)"      # current zsh session
    """
    # Imported lazily to avoid a circular import with cosmo.main.
    from cosmo.main import cli

    comp_cls = get_completion_class(shell)
    if comp_cls is None:  # pragma: no cover - all three shells are supported
        raise click.ClickException(f"No completion support for shell {shell!r}.")

    comp = comp_cls(cli, {}, _PROG_NAME, _COMPLETE_VAR)

    if not no_hint:
        click.echo(_INSTALL_HINTS[shell])
        click.echo()
    click.echo(comp.source())
