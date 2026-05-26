"""
cosmo — Cosmonapse developer CLI

Commands
--------
cosmo synapse    Manage Synapse servers (start / view / stop)
cosmo doppler    Attach a Doppler to the Synapse, stream Signals to stdout
cosmo validate   Validate that Signals on the Synapse conform to the envelope spec
"""

import click

from cosmo.commands.doppler import doppler
from cosmo.commands.synapse import synapse
from cosmo.commands.validate import validate


@click.group()
@click.version_option(package_name="cosmonapse", prog_name="cosmo")
def cli() -> None:
    """Cosmonapse developer tooling."""


cli.add_command(synapse)
cli.add_command(doppler)
cli.add_command(validate)


if __name__ == "__main__":
    cli()
