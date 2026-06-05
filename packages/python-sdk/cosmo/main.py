"""
cosmo  -  Cosmonapse developer CLI

Commands
--------
cosmo init        Scaffold a minimal Axon + Dendrite project
cosmo synapse     Manage Synapse servers (start / view / stop)
cosmo doppler     Attach a Doppler to the Synapse, stream Signals to stdout
cosmo validate    Validate that Signals on the Synapse conform to the envelope spec
cosmo completion  Print a shell-completion script (bash / zsh / fish)
"""

import click

from cosmo.commands.completion import completion
from cosmo.commands.doppler import doppler
from cosmo.commands.init import init
from cosmo.commands.synapse import synapse
from cosmo.commands.validate import validate


@click.group()
@click.version_option(package_name="cosmonapse", prog_name="cosmo")
def cli() -> None:
    """Cosmonapse developer tooling."""


cli.add_command(init)
cli.add_command(synapse)
cli.add_command(doppler)
cli.add_command(validate)
cli.add_command(completion)


if __name__ == "__main__":
    cli()
