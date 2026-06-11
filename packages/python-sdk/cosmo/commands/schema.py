"""
cosmo schema
~~~~~~~~~~~~
Export the frozen protocol surface as JSON Schema, so non-SDK
implementations (any language) can validate Signals against the spec
without reading Python.

Usage
-----
    cosmo schema                 # envelope JSON Schema to stdout
    cosmo schema --types         # list every SignalType instead
    cosmo schema -o envelope.schema.json
"""

from __future__ import annotations

import json

import click

from cosmonapse import Signal, SignalType


@click.command()
@click.option("--types", "list_types", is_flag=True, default=False,
              help="List the SignalType vocabulary instead of the schema.")
@click.option("--output", "-o", default=None, metavar="PATH",
              help="Write to a file instead of stdout.")
def schema(list_types: bool, output: str | None) -> None:
    """Print the Signal envelope JSON Schema (protocol major version 1)."""
    if list_types:
        body = json.dumps([t.value for t in SignalType], indent=2)
    else:
        body = json.dumps(Signal.model_json_schema(), indent=2)
    if output:
        with open(output, "w", encoding="utf-8") as fh:
            fh.write(body + "\n")
        click.echo(f"wrote {output}")
    else:
        click.echo(body)
