"""
examples/neuron_providers/main.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Shows how to use Neuron(source=...) provider wrappers to drop an
LLM into a workflow without writing any HTTP code.

Requires:
    pip install httpx
    # And a HuggingFace token (read scope is enough):
    export HF_TOKEN=hf_xxxxxxxxxxxxxxxxxxxxxxxx   # https://huggingface.co/settings/tokens
    # Or, for the commented Ollama alternative:
    ollama pull llama3

Run with MemorySynapse (no external broker needed):
    python examples/neuron_providers/main.py
"""

import asyncio
import os

from cosmonapse import (
    Axon,
    Dendrite,
    MemoryRegistryStore,
    MemorySynapse,
    Neuron,
)


# ---------------------------------------------------------------------------
# 1.  Pick your provider (swap freely  -  the rest of the code is identical)
# ---------------------------------------------------------------------------

# Option A – HuggingFace Inference Providers router (default; needs HF_TOKEN)
llm_fn = Neuron(
    source="huggingface",
    endpoint="https://router.huggingface.co",
    model="meta-llama/Llama-3.1-8B-Instruct",
    api_key=os.environ["HF_TOKEN"],
    use_chat_api=True,
)

# Option B – Ollama running locally (swap source, everything else is identical)
# llm_fn = Neuron(source="ollama", model="llama3")

# Option C – Self-hosted HF TGI / vLLM / LM Studio / llama.cpp --server
# llm_fn = Neuron(source="huggingface", endpoint="http://localhost:8080")

# Option D – Dedicated HF Inference Endpoint with an auth token
# llm_fn = Neuron(
#     source="huggingface",
#     endpoint="https://<your-endpoint>.endpoints.huggingface.cloud",
#     api_key=os.environ["HF_TOKEN"],
#     use_chat_api=True,
# )


# ---------------------------------------------------------------------------
# 2.  Wrap in an Axon  -  nothing else changes vs. a hand-written neuron_fn
# ---------------------------------------------------------------------------

axon = Axon(
    neuron_id="llm-chat",
    neuron_fn=llm_fn,
    capabilities=["text-generation", "chat"],
)


# ---------------------------------------------------------------------------
# 3.  Wire up a minimal workflow with MemorySynapse (no broker required)
# ---------------------------------------------------------------------------

async def main():
    synapse = MemorySynapse()
    store   = MemoryRegistryStore()

    worker = Dendrite(synapse=synapse, namespace="demo")
    worker.attach_axon(axon)

    orch = Dendrite(synapse=synapse, registry_store=store, namespace="demo")

    result: dict = {}

    @orch.on_agent_output
    async def on_output(sig):
        result["response"] = sig.payload["output"]["response"]
        await orch.emit_final(
            trace_id=sig.trace_id,
            parent_id=sig.id,
     