"""
cosmo answer
~~~~~~~~~~~~
Be the human in the loop, from the terminal.

Attaches to a running Synapse and watches for CLARIFICATION and PERMISSION
requests. Each request is shown interactively: type an answer to a
clarification, approve/deny a permission. Replies are sent as discrete
CLARIFICATION_ANSWER / PERMISSION_DECISION signals by default, or as
re-dispatched follow-up TASKs with --redispatch (the close-the-loop flow
that makes the asking Neuron run again).

Usage
-----
    cosmo answer --url=cosmo://127.0.0.1:7070 -n dev
    cosmo answer --url=... -n dev --trace trc_01H...     # one workflow only
    cosmo answer --url=... -n dev --redispatch
"""

from __future__ import annotations

import asyncio
import json

import click

from cosmonapse import Dendrite, Signal, SignalType, connect_synapse


@click.command()
@click.option("--url", required=True, metavar="URL",
              help="Synapse URL (cosmo:// | nats:// | kafka://).")
@click.option("--namespace", "-n", default="dev", show_default=True)
@click.option("--trace", default=None, metavar="TRACE_ID",
              help="Only answer requests on this trace.")
@click.option("--redispatch", is_flag=True, default=False,
              help="Reply by re-dispatching a follow-up TASK to the asking "
                   "Neuron (respond_to_clarification / respond_to_permission) "
                   "instead of emitting a discrete answer signal.")
def answer(url: str, namespace: str, trace: str | None,
           redispatch: bool) -> None:
    """Interactively answer CLARIFICATION / PERMISSION requests."""
    try:
        asyncio.run(_run_answer(url, namespace, trace, redispatch))
    except KeyboardInterrupt:
        click.echo("\nbye")


async def _run_answer(url: str, namespace: str, trace: str | None,
                      redispatch: bool) -> None:
    synapse = await connect_synapse(url)
    d = Dendrite(synapse=synapse, namespace=namespace,
                 dendrite_id="cosmo-answer", heartbeat_s=0)
    queue: asyncio.Queue[Signal] = asyncio.Queue()

    @d.on_clarification(trace_id=trace)
    async def _clar(sig: Signal) -> None:
        await queue.put(sig)

    @d.on_permission(trace_id=trace)
    async def _perm(sig: Signal) -> None:
        await queue.put(sig)

    try:
        async with d:
            click.echo(
                f"watching namespace {namespace!r} for CLARIFICATION / "
                f"PERMISSION{f' on trace {trace}' if trace else ''} "
                f"(Ctrl-C to quit)"
            )
            loop = asyncio.get_running_loop()
            while True:
                sig = await queue.get()
                # click.prompt blocks; run it off the event loop so signal
                # delivery continues while the human types.
                await loop.run_in_executor(
                    None, _handle_sync, d, sig, redispatch, loop,
                )
    finally:
        await synapse.close()


def _handle_sync(d: Dendrite, sig: Signal, redispatch: bool,
                 loop: asyncio.AbstractEventLoop) -> None:
    asker = sig.directed.id if sig.directed else "?"
    if sig.type is SignalType.CLARIFICATION:
        click.echo(f"\n[clarification] from {asker}  trace={sig.trace_id}")
        click.echo(f"  question: {sig.payload.get('question')}")
        if sig.payload.get("context"):
            click.echo(f"  context:  {json.dumps(sig.payload['context'])}")
        reply = click.prompt("  answer")
        coro = (
            d.respond_to_clarification(sig, answer=reply)
            if redispatch else d.answer_clarification(sig, answer=reply)
        )
    else:
        click.echo(f"\n[permission] from {asker}  trace={sig.trace_id}")
        click.echo(f"  action: {sig.payload.get('action')}")
        if sig.payload.get("scope"):
            click.echo(f"  scope:  {json.dumps(sig.payload['scope'])}")
        if sig.payload.get("reason"):
            click.echo(f"  reason: {sig.payload.get('reason')}")
        granted = click.confirm("  grant?", default=False)
        if redispatch:
            coro = d.respond_to_permission(sig, granted=granted)
        elif granted:
            coro = d.grant_permission(sig)
        else:
            coro = d.deny_permission(sig)
    asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=10.0)
    click.echo("  sent.")
