"""
cosmo genesis
~~~~~~~~~~~~~
Open Genesis: the browser wizard for starting a new brain. Name the project,
pick a folder, scaffold it (the same standard skeleton as `cosmo init`), then
see it as a draw.io-style canvas - one Synapse with the Neurons, Effectors,
and Engram it hosts laid out around it.

Usage
-----
    cosmo genesis
    cosmo genesis --port=8080

The heavy lifting (SPA + local API for folder browsing and scaffolding)
lives in cosmo/commands/_genesis.py, the same split doppler/_prism.py uses.
"""

from __future__ import annotations

import asyncio

import click

from cosmo.commands._genesis import run_genesis as _run_genesis


@click.command()
@click.option("--port", default=7072, show_default=True,
              help="Local port for the Genesis server.")
def genesis(port: int) -> None:
    """Launch Genesis: name a brain, pick a folder, scaffold it, see the canvas.

    \b
    Examples:
      cosmo genesis
      cosmo genesis --port=8080
    """
    asyncio.run(_run_genesis(port=port))
