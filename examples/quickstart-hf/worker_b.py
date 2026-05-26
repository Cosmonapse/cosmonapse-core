"""
worker_b.py
~~~~~~~~~~~
Second Hugging Face worker. Same shape as worker_a.py -- different
``neuron_id`` and a different model so you can tell the responses apart.

Run order:
    1.  cosmo synapse start memory --namespace=quickstart
    2.  set HF_TOKEN=hf_xxx     (Windows)
        export HF_TOKEN=hf_xxx  (macOS / Linux)
    3.  python worker_b.py
"""

import asyncio
import os

from cosmonapse import Axon, Dendrite, Neuron, connect_synapse

SYNAPSE_URL = "cosmo://127.0.0.1:7070"
NAMESPACE   = "quickstart"

HF_ENDPOINT = "https://router.huggingface.co"
HF_MODEL    = "Qwen/Qwen2.5-7B-Instruct"


async def main() -> None:
    neuron_fn = Neuron(
        source         = "huggingface",
        endpoint       = HF_ENDPOINT,
        model          = HF_MODEL,
        api_key        = os.environ["HF_TOKEN"],
        use_chat_api   = True,
        max_new_tokens = 128,
        temperature    = 0.7,
    )

    axon = Axon(
        neuron_id    = "hf-worker-b",
        neuron_fn    = neuron_fn,
        capabilities = ["text-generation", "chat"],
        version      = "0.0.1",
    )

    synapse  = await connect_synapse(SYNAPSE_URL)
    dendrite = Dendrite(
        synapse     = synapse,
        namespace   = NAMESPACE,
        dendrite_id = "worker-b",
    )
    dendrite.attach_axon(axon)

    try:
        async with dendrite:
            print("worker-b ready  (neuron_id=hf-worker-b)  -- Ctrl-C to stop")
            await asyncio.Event().wait()
    finally:
        await synapse.close()


if __name__ == "__main__":
    asyncio.run(main())
