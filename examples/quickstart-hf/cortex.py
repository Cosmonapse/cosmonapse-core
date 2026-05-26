"""
cortex.py
~~~~~~~~~
Cortex = orchestrator Dendrite. Owns no Axon — its only job is to
dispatch TASK Signals and collect AGENT_OUTPUT Signals.

This Cortex round-robins prompts across a static pool of worker
neuron IDs. Run order:

    1.  cosmo synapse start memory --namespace=quickstart
    2.  python worker_a.py
    3.  python worker_b.py
    4.  python cortex.py     ← this file
"""

import asyncio
import itertools

from cosmonapse import Dendrite, connect_synapse, new_trace_id

SYNAPSE_URL = "cosmo://127.0.0.1:7070"
NAMESPACE   = "quickstart"

# Round-robin pool. Add another worker → add its neuron_id here.
WORKERS = ("hf-worker-a", "hf-worker-b")


class RoundRobinCortex:
    """A Dendrite wrapper that round-robins requests across a worker pool."""

    def __init__(self, dendrite: Dendrite, workers: tuple[str, ...]) -> None:
        self._dendrite = dendrite
        self._cycle    = itertools.cycle(workers)
        # trace_id → Future, resolved by the AGENT_OUTPUT handler below.
        self._pending: dict[str, asyncio.Future[dict]] = {}

        @dendrite.on_agent_output
        async def _on_output(sig):
            fut = self._pending.pop(sig.trace_id, None)
            if fut and not fut.done():
                fut.set_result(sig.payload.get("output", {}))

        @dendrite.on_error_signal
        async def _on_error(sig):
            fut = self._pending.pop(sig.trace_id, None)
            if fut and not fut.done():
                fut.set_exception(
                    RuntimeError(sig.payload.get("message", "neuron error"))
                )

    async def ask(self, prompt: str, *, timeout: float = 60.0) -> dict:
        target   = next(self._cycle)           # ← round-robin pick
        trace_id = new_trace_id()
        fut      = asyncio.get_running_loop().create_future()
        self._pending[trace_id] = fut

        await self._dendrite.dispatch_task(
            neuron   = target,
            input    = {"prompt": prompt},
            trace_id = trace_id,
        )
        print(f"→ dispatched to {target}  trace={trace_id[4:12]}")
        return await asyncio.wait_for(fut, timeout=timeout)


async def main() -> None:
    synapse  = await connect_synapse(SYNAPSE_URL)
    dendrite = Dendrite(
        synapse     = synapse,
        namespace   = NAMESPACE,
        dendrite_id = "cortex",
        heartbeat_s = 0,                       # cortex hosts no axons
    )
    cortex = RoundRobinCortex(dendrite, WORKERS)

    prompts = [
        "Write a one-line haiku about the sun.",
        "Write a one-line haiku about the moon.",
        "Write a one-line haiku about the sea.",
        "Write a one-line haiku about the wind.",
    ]

    try:
        async with dendrite:
            for p in prompts:
                result = await cortex.ask(p)
                print(f"   ← {result.get('response', '').strip()}\n")
    finally:
        await synapse.close()


if __name__ == "__main__":
    asyncio.run(main())
