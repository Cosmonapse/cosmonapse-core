"""
step2_worker.py
~~~~~~~~~~~~~~~
Introduces three core primitives in one file:

    Neuron    -  a pure async function; it has zero knowledge of the protocol.
    Axon      -  wraps the neuron, turns its output into a protocol-valid Signal.
    Dendrite  -  connects the Axon to the Synapse, handles REGISTER / heartbeat /
               TASK routing.

Run after the synapse is up (step1):

    python step2_worker.py

You'll see:
    ✓  Worker ready  -  neuron 'hello-neuron' registered on namespace 'quickstart'

The worker then waits silently for TASK Signals.  Send one with step4, or through
the Flask server in step3, and you'll see it process the task.
"""

import asyncio
import signal as _signal

from cosmonapse import Axon, Dendrite, connect_synapse

SYNAPSE_URL = "cosmo://127.0.0.1:7070"
NAMESPACE   = "quickstart"


# ---------------------------------------------------------------------------
# Step 2  -  The Neuron
#
# A Neuron is just an async function: (input: dict, context: list) -> dict.
# No imports from cosmonapse, no Signal knowledge, no envelope boilerplate.
# ---------------------------------------------------------------------------

async def hello_neuron(input: dict, context: list) -> dict:
    """Greet whoever sent the task."""
    name = input.get("name", "world")
    return {"message": f"Hello, {name}!"}


# ---------------------------------------------------------------------------
# Step 3  -  The Axon
#
# Axon wraps hello_neuron and gives it an identity on the bus:
#   - neuron_id      → the address other Dendrites use to dispatch tasks here
#   - capabilities   → advertised skills (used for capability-based routing)
#   - version        → included in the REGISTER Signal so dashboards can show it
# ---------------------------------------------------------------------------

axon = Axon(
    neuron_id   = "hello-neuron",
    neuron_fn   = hello_neuron,
    capabilities = ["greet"],
    version     = "0.0.1",
)


# ---------------------------------------------------------------------------
# Step 4  -  Dendrite + Synapse
#
# Dendrite is the synapse-side participant.  It:
#   - connects to the Synapse (message bus)
#   - emits REGISTER on behalf of each attached Axon
#   - subscribes to TASK and routes inbound tasks to the correct Axon
#   - emits HEARTBEAT on a timer so the registry stays fresh
# ---------------------------------------------------------------------------

async def main() -> None:
    synapse = await connect_synapse(SYNAPSE_URL)

    try:
        dendrite = Dendrite(
            synapse                 = synapse,
            namespace               = NAMESPACE,
            dendrite_id             = "hello-worker",
            heartbeat_s             = 15.0,
            reregister_on_heartbeat = False,
        )
        dendrite.attach_axon(axon)

        # Optional lifecycle hooks ----------------------------------------
        @axon.on_connect
        async def _on_connect():
            print(f"  [axon] 'hello-neuron' registered on namespace {NAMESPACE!r}")

        async with dendrite:
            print(f"✓  Worker ready  -  listening for tasks on namespace {NAMESPACE!r}")
            print("   Press Ctrl+C to stop.\n")

            stop = asyncio.Event()
            loop = asyncio.get_running_loop()

            def _on_signal(*_):
                loop.call_soon_threadsafe(stop.set)

            for sig in (getattr(_signal, "SIGINT", None), getattr(_signal, "SIGTERM", None)):
                if sig is not None:
                    try:
                        loop.add_signal_handler(sig, _on_signal)
                    except (NotImplementedError, RuntimeError):
                        pass

            try:
                await stop.wait()
            except (KeyboardInterrupt, asyncio.CancelledError):
                pass

    finally:
        await synapse.close()
        print("   Worker stopped.")


if __name__ == "__main__":
    asyncio.run(main())
