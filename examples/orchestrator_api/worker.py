"""
examples/orchestrator_api/worker.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The shared worker process used by all framework examples in this directory.

Run this before starting any of the web framework examples. It connects to the
dev Synapse, registers a single Neuron under the id "worker", and processes
every TASK dispatched to it.

    cosmo synapse start memory --namespace=api-demo   # terminal 1
    python examples/orchestrator_api/worker.py         # terminal 2
"""
from __future__ import annotations

import asyncio
import os

from cosmonapse import Axon, Dendrite, Neuron, connect_synapse

SYNAPSE_URL = os.environ.get("SYNAPSE_URL", "cosmo://127.0.0.1:7070")
NAMESPACE   = "api-demo"


async def main() -> None:
    neuron = Neuron(
        source="huggingface",
        endpoint="https://router.huggingface.co",
        model="meta-llama/Llama-3.1-8B-Instruct",
        api_key=os.environ["HF_TOKEN"],
        use_chat_api=True,
        max_new_tokens=256,
    )

    axon = Axon(
        neuron_id="worker",
        neuron_fn=neuron,
        capabilities=["text-generation", "chat"],
    )

    synapse  = await connect_synapse(SYNAPSE_URL)
    dendrite = Dendrite(synapse=synapse, namespace=NAMESPACE, dendrite_id="worker")
    dendrite.attach_axon(axon)

    try:
        async with dendrite:
            print(f"worker ready on {SYNAPSE_URL} / namespace={NAMESPACE}")
            await asyncio.Event().wait()
    finally:
        await synapse.close()


if __name__ == "__main__":
    asyncio.run(main())
