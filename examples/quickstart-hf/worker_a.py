"""
worker_a.py
~~~~~~~~~~~
First Hugging Face worker.

A Dendrite that hosts a single Axon whose Neuron is backed by the
Hugging Face Inference Providers router.

Run order:
    1.  cosmo synapse start memory --namespace=quickstart
    2.  set HF_TOKEN=hf_xxx     (Windows)
        export HF_TOKEN=hf_xxx  (macOS / Linux)
    3.  python worker_a.py
"""

import asyncio
import os

from cosmonapse import Axon, Dendrite, Neuron, connect_synapse

SYNAPSE_URL = "cosmo://127.0.0.1:7070"
NAMESPACE   = "quickstart"

# Base URL only -- the SDK appends `/v1/chat/completions`.
#   Inference Providers router (recommended):  https://router.huggingface.co
#   Dedicated Inference Endpoint:              https://<your-endpoint>.endpoints.huggingface.cloud
#   Local TGI / vLLM:                          http://localhost:8080
HF_ENDPOINT = "https://router.huggingface.co"

# Model id goes in the request body. Optional provider suffix pins the
# upstream provider, e.g. ":cerebras", ":groq", ":fastest", ":cheapest".
HF_MODEL = "meta-llama/Llama-3.1-8B-Instruct"


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
        neuron_id    = "hf-worker-a",
        neuron_fn    = neuron_fn,
        capabilities = ["text-generation", "chat"],
        version      = "0.0.1",
    )

    synapse  = await connect_synapse(SYNAPSE_URL)
    dendrite = Dendrite(
        synapse     = synapse,
        namespace   = NAMESPACE,
        dendrite_id = "worker-a",
    )
    dendrite.attach_axon(axon)

    try:
        async with dendrite:
            print("worker-a ready  (neuron_id=hf-worker-a)  -- Ctrl-C to stop")
            await asyncio.Event().wait()
    finally:
        await synapse.close()


if __name__ == "__main__":
    asyncio.run(main())
