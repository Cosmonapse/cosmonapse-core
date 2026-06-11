"""
cosmo registry
~~~~~~~~~~~~~~
Inspect the live Neuron population of a namespace without writing code.

Works store-free: emits DISCOVER on the Synapse and collects the REGISTER
replies every hosting Dendrite sends back, deduplicated by neuron_id.

Usage
-----
    cosmo registry list --url=cosmo://127.0.0.1:7070 -n dev
    cosmo registry list --url=... -n dev --capability summarize --json
"""

from __future__ import annotations

import asyncio
import json

import click

from cosmonapse import Dendrite, SignalType, connect_synapse

from cosmo.commands._shared import _HAS_RICH

if _HAS_RICH:
    from rich.console import Console
    from rich.table import Table


@click.group()
def registry() -> None:
    """Inspect the Neuron registry of a namespace."""


@registry.command(name="list")
@click.option("--url", required=True, metavar="URL",
              help="Synapse URL (cosmo:// | nats:// | kafka://).")
@click.option("--namespace", "-n", default="dev", show_default=True)
@click.option("--capability", default=None, metavar="CAP",
              help="Only Neurons declaring this capability.")
@click.option("--timeout", "timeout_s", default=1.5, show_default=True,
              help="Seconds to collect REGISTER replies after DISCOVER.")
@click.option("--json", "output_json", is_flag=True, default=False)
def list_cmd(url: str, namespace: str, capability: str | None,
             timeout_s: float, output_json: bool) -> None:
    """Emit DISCOVER and print every Neuron that announces itself."""
    try:
        asyncio.run(_run_list(url, namespace, capability, timeout_s,
                              output_json))
    except KeyboardInterrupt:
        raise SystemExit(130)


async def _run_list(url, namespace, capability, timeout_s,
                    output_json) -> None:
    synapse = await connect_synapse(url)
    d = Dendrite(synapse=synapse, namespace=namespace,
                 dendrite_id="cosmo-registry", heartbeat_s=0)
    seen: dict[str, dict] = {}

    @d.on_register_signal
    async def _collect(sig) -> None:
        nid = sig.directed.id if sig.directed else None
        if not nid:
            return
        role = sig.payload.get("role") or (
            "engram" if sig.payload.get("engram") else "neuron")
        seen[nid] = {
            "id": nid,
            "kind": (sig.directed.type if sig.directed else None) or role,
            "role": role,
            "capabilities": sorted(
                sig.payload.get("capabilities")
                or (sig.directed.capabilities if sig.directed else [])
                or []
            ),
            "version": sig.payload.get("version"),
        }

    try:
        async with d:
            await d.ensure_subscribed(SignalType.REGISTER)
            await d._emit_discover(capabilities=[capability] if capability else None)
            await asyncio.sleep(timeout_s)
    finally:
        await synapse.close()

    records = sorted(seen.values(), key=lambda r: r["id"])
    if capability:
        records = [r for r in records if capability in r["capabilities"]]
    if output_json:
        click.echo(json.dumps(records, indent=2))
        return
    if not records:
        click.echo("no participants answered DISCOVER "
                   f"(namespace={namespace!r}, waited {timeout_s}s)")
        return
    if _HAS_RICH:
        table = Table(title=f"namespace {namespace!r}")
        for col in ("id", "role", "kind", "capabilities", "version"):
            table.add_column(col)
        for r in records:
            table.add_row(r["id"], r["role"], str(r["kind"]),
                          ",".join(r["capabilities"]), str(r["version"] or "-"))
        Console().print(table)
    else:
        for r in records:
            click.echo(f"{r['id']}  [{r['role']}/{r['kind']}]  "
                       f"caps={','.join(r['capabilities'])}  "
                       f"v={r['version'] or '-'}")
