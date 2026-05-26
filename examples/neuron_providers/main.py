"""
examples/neuron_providers/main.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Shows how to use Neuron(source=...) provider wrappers to drop an
LLM into a workflow without writing any HTTP code.

Requires:
    pip install httpx
    # And one of:
    ollama pull llama3          # for the Ollama example
    docker run … ghcr.io/huggingface/text-generation-inference ...  # for HF TGI

Run with MemorySynapse (no external broker needed):
    python examples/neuron_providers/main.py
"""

import asyncio

from cosmonapse import (
    Axon,
    Dendrite,
    MemoryRegistryStore,
    MemorySynapse,
    Neuron,
)


# ---------------------------------------------------------------------------
# 1.  Pick your provider (swap freely — the rest of the code is identical)
# ---------------------------------------------------------------------------

# Option A – Ollama running locally
ollama_fn = Neuron(source="ollama", model="llama3")

# Option B – HuggingFace TGI (or vLLM / LM Studio / llama.cpp --server)
# hf_fn = Neuron(source="huggingface", endpoint="http://localhost:8080")

# Option C – Hosted HF Inference Endpoint with an auth token
# hf_fn = Neuron(
#     source="huggingface",
#     endpoint="https://<your-endpoint>.endpoints.huggingface.cloud",
#     api_key="hf_…",
#     use_chat_api=True,
# )


# ---------------------------------------------------------------------------
# 2.  Wrap in an Axon — nothing else changes vs. a hand-written neuron_fn
# ---------------------------------------------------------------------------

axon = Axon(
    neuron_id="llm-chat",
    neuron_fn=ollama_fn,
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
            result=sig.payload["output"],
        )

    async with orch, worker:
        await orch.dispatch_task(
            neuron="llm-chat",
            input={"prompt": "Explain cosmonapse in one sentence."},
        )
        await asyncio.sleep(30)   # give the LLM time to respond

    print("Response:", result.get("response", "<no response>"))


if __name__ == "__main__":
    asyncio.run(main())
