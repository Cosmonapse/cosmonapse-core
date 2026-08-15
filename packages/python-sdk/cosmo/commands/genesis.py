"""
cosmo genesis
~~~~~~~~~~~~~
Open Genesis: the browser wizard for starting a new brain. Name the project,
pick a folder, scaffold it (the same standard skeleton as `cosmo init`), then
see it as a draw.io-style canvas - one Synapse with the Neurons, Engrams,
and Effectors it hosts laid out around it, each wearing the same silhouette
Prism gives it. Add components from the canvas (Genesis writes the module and
wires it into brain.py) and read every one of them in the Code tab. Run the
result from the Test tab, and keep it under version control from the History
tab - Genesis rewrites brain.py on every add and remove, so a repository is
the undo for most of what it does. The start screen will also clone a project
from GitHub or GitLab; the token for that goes to git's own credential store,
never to a file Genesis writes.

Usage
-----
    cosmo genesis
    cosmo genesis --port=8080

The heavy lifting (SPA + local API for folder browsing and scaffolding)
lives in cosmo/commands/_genesis.py, the same split prism/_prism.py uses;
the pieces that spawn or shell out sit beside it in _genesis_run.py,
_genesis_synapse.py, _genesis_git.py and _genesis_forge.py.
"""

from __future__ import annotations

import asyncio

import click

from cosmo.commands._genesis import run_genesis as _run_genesis


@click.command()
@click.option("--port", default=7072, show_default=True,
              help="Local port for the Genesis server.")
def genesis(port: int) -> None:
    """Launch Genesis: name a brain, pick a folder, scaffold it, grow it on a canvas.

    \b
    Examples:
      cosmo genesis
      cosmo genesis --port=8080
    """
    asyncio.run(_run_genesis(port=port))
