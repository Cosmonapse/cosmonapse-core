"""
cosmo dispatch
~~~~~~~~~~~~~~
Dispatch a TASK onto a running Synapse from the terminal and (by default)
wait for the result. The fastest way to exercise the default dispatch +
wait flow without writing an orchestrator.

Usage
-----
    cosmo dispatch --url=cosmo://127.0.0.1:7070 -n dev --neuron hello \\
        --input '{"name": "Cosmonapse"}'

    # capability-routed
    cosmo dispatch --url=... -n dev --capabilities summarize,english \\
        --input '{"text": "..."}'

    # competitive bidding (TASK_OFFER / BID / TASK_AWARDED)
    cosmo dispatch --url=... -n dev --offer --capabilities summarize \\
        --input '{"text": "..."}'

    # fire-and-forget
    cosmo dispatch --url=... -n dev --neuron hello --input '{}' --no-wait
"""

from __future__ import annotations

import asyncio
import json

import click

from cosmonapse import Dendrite, SignalType, connect_synapse


@click.command()
@click.option("--url", required=True, metavar="URL",
              help="Synapse URL (cosmo:// | nats:// | kafka://).")
@click.option("--namespace", "-n", default="dev", show_default=True,
              help="Namespace to dispatch into.")
@click.option("--neuron", default=None, metavar="ID",
              help="Addressed dispatch: the target Axon's neuron_id.")
@click.option("--capabilities", default=None, metavar="A,B",
              help="Capability-routed dispatch: comma-separated capability list.")
@click.option("--input", "input_json", default="{}", show_default=True,
              metavar="JSON", help="TASK input as a JSON object.")
@click.option("--offer", is_flag=True, default=False,
              help="Use TASK_OFFER / BID / TASK_AWARDED instead of direct dispatch.")
@click.option("--deadline-ms", default=250, show_default=True,
              help="Bid-collection window for --offer.")
@click.option("--select", default="first_bid", show_default=True,
              type=click.Choice(["first_bid", "lowest_cost", "highest_confidence"]),
              help="Winner-selection strategy for --offer.")
@click.option("--wait/--no-wait", default=True, show_default=True,
              help="Wait for the reply (FINAL via terminal-handler finalize).")
@click.option("--timeout", "timeout_s", default=30.0, show_default=True,
              help="Seconds to wait for a reply.")
@click.option("--scope", default="terminal", show_default=True,
              type=click.Choice(["all", "terminal"]),
              help="Pathway scope while waiting.")
@click.option("--json", "output_json", is_flag=True, default=False,
              help="Print the raw reply Signal as JSON (machine-readable).")
def dispatch(url: str, namespace: str, neuron: str | None,
             capabilities: str | None, input_json: str, offer: bool,
             deadline_ms: int, select: str, wait: bool, timeout_s: float,
             scope: str, output_json: bool) -> None:
    """Dispatch a TASK and print the reply."""
    try:
        input_data = json.loads(input_json)
    except ValueError as exc:
        raise click.BadParameter(f"--input is not valid JSON: {exc}")
    if not isinstance(input_data, dict):
        raise click.BadParameter("--input must be a JSON object")
    caps = [c.strip() for c in capabilities.split(",") if c.strip()] \
        if capabilities else None
    if not offer and neuron is None and not caps:
        raise click.UsageError(
            "provide --neuron (addressed) or --capabilities (routed), "
            "or use --offer"
        )
    try:
        asyncio.run(_run_dispatch(
            url, namespace, neuron, caps, input_data, offer,
            deadline_ms, select, wait, timeout_s, scope, output_json,
        ))
    except KeyboardInterrupt:
        raise SystemExit(130)


async def _run_dispatch(url, namespace, neuron, caps, input_data, offer,
                        deadline_ms, select, wait, timeout_s, scope,
                        output_json) -> None:
    synapse = await connect_synapse(url)
    orch = Dendrite(synapse=synapse, namespace=namespace,
                    dendrite_id="cosmo-cli", heartbeat_s=0)
    try:
        async with orch:
            if offer:
                pathway = await orch.dispatch_offer(
                    input=input_data, capabilities=caps,
                    deadline_ms=deadline_ms, select=select, scope=scope,
                )
                if not wait:
                    await pathway.close()
                    click.echo(f"offer awarded  trace={pathway.trace_id}")
                    return
                async with pathway as pw:
                    sig = await pw.wait(timeout_s=timeout_s)
            elif wait:
                sig = await orch.dispatch_and_wait(
                    neuron=neuron, input=input_data, capabilities=caps,
                    scope=scope, timeout_s=timeout_s,
                )
            else:
                sig = await orch.dispatch_task(
                    neuron=neuron, input=input_data, capabilities=caps,
                )
                click.echo(f"dispatched  trace={sig.trace_id}  id={sig.id}")
                return
            _print_reply(sig, output_json)
            if sig.type is SignalType.ERROR:
                raise SystemExit(1)
    except asyncio.TimeoutError:
        click.echo(f"timed out after {timeout_s}s waiting for a reply",
                   err=True)
        raise SystemExit(2)
    finally:
        await synapse.close()


def _print_reply(sig, output_json: bool) -> None:
    if output_json:
        click.echo(sig.model_dump_json())
        return
    who = sig.directed.id if sig.directed else "?"
    click.echo(f"{sig.type.value}  trace={sig.trace_id}  from={who}")
    body = sig.payload.get("result", sig.payload.get("output", sig.payload))
    click.echo(json.dumps(body, indent=2, default=str))
