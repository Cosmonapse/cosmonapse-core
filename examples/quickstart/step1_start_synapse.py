"""
step1_start_synapse.py
~~~~~~~~~~~~~~~~~~~~~~
Start a local DevSynapseServer — the message bus every other process connects to.

Run this first, in its own terminal:

    python step1_start_synapse.py

You'll see:
    ✓  Synapse listening at cosmo://127.0.0.1:7070

Leave it running. All workers and servers in this quickstart connect to this URL.

DevSynapseServer is the zero-infrastructure option (TCP + NDJSON, no external
dependencies). Swap in NatsSynapse or KafkaSynapse with a one-line URL change
when you're ready to go to production.
"""

import asyncio
import signal as _signal

from cosmonapse.synapse.dev import DevSynapseServer

SYNAPSE_HOST = "127.0.0.1"
SYNAPSE_PORT = 7070


async def main() -> None:
    server = DevSynapseServer(host=SYNAPSE_HOST, port=SYNAPSE_PORT)
    await server.start()
    print(f"✓  Synapse listening at {server.url}")
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
                pass  # Windows fallback

    try:
        await stop.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        print("\n   Stopping synapse…")
        await server.stop()
        print("   Stopped.")


if __name__ == "__main__":
    asyncio.run(main())
