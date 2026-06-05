# cosmonapse

Event-driven Agent-to-Agent protocol SDK for Python.

**v0.1.0**  -  Python 3.11+ · MIT

---

## Install

```bash
pip install cosmonapse

# Optional synapse adapters
pip install "cosmonapse[nats]"      # NATS transport
pip install "cosmonapse[kafka]"     # Kafka transport (aiokafka)

# Optional storage backend
pip install "cosmonapse[postgres]"  # Postgres registry store (asyncpg)

# Provider-backed Neurons (Ollama / HuggingFace / OpenAI / Anthropic / groq /
# openrouter / together / mistral) need httpx
pip install httpx
```

---

## Quick start

```python
import asyncio
from cosmonapse import (
    Axon, Dendrite, Neuron,
    MemoryRegistryStore,
    connect_synapse,
)

async def main():
    # 1. Connect to a running synapse (cosmo synapse start memory)
    synapse = await connect_synapse("cosmo://127.0.0.1:7070")
    try:
        # 2. Define a pure agent function (zero protocol knowledge)
        async def my_agent(input, context):
            return {"answer": f"You asked: {input['q']}"}

        # -- Worker Dendrite: hosts an Axon --------------------------
        worker = Dendrite(synapse=synapse, namespace="demo")
        worker.attach_axon(Axon(neuron_id="answerer", neuron_fn=my_agent))

        # -- Orchestrator Dendrite: drives the workflow ---------------
        orch = Dendrite(
            synapse=synapse,
            registry_store=MemoryRegistryStore(),
            namespace="demo",
        )

        @orch.on_agent_output
        async def done(sig):
            print("Got answer:", sig.payload["output"])
            await orch.emit_final(
                trace_id=sig.trace_id,
                parent_id=sig.id,
                result=sig.payload["output"],
            )

        async with orch, worker:
            await orch.dispatch_task(neuron="answerer", input={"q": "hi"})
            await asyncio.sleep(0.5)
    finally:
        await synapse.close()

asyncio.run(main())
```

Start the dev synapse first:

```bash
cosmo synapse start memory --namespace=demo
```

---

## Provider-backed Neurons

Drop an LLM into any workflow with `Neuron(source=...)`:

```python
from cosmonapse import Axon, Neuron

# Local Ollama daemon
axon = Axon(
    neuron_id="chat",
    neuron_fn=Neuron(source="ollama", model="llama3"),
)

# HuggingFace TGI / vLLM / llama.cpp / LM Studio
axon = Axon(
    neuron_id="summariser",
    neuron_fn=Neuron(
        source="huggingface",
        endpoint="http://localhost:8080",
    ),
)

# HuggingFace hosted endpoint (requires api_key)
axon = Axon(
    neuron_id="classifier",
    neuron_fn=Neuron(
        source="huggingface",
        endpoint="https://<your-endpoint>.endpoints.huggingface.cloud",
        api_key="hf_…",
        use_chat_api=True,
    ),
)

# OpenAI (api_key falls back to OPENAI_API_KEY)
axon = Axon(
    neuron_id="writer",
    neuron_fn=Neuron(source="openai", model="gpt-4o", system="Be concise."),
)

# Anthropic (api_key falls back to ANTHROPIC_API_KEY)
axon = Axon(
    neuron_id="reasoner",
    neuron_fn=Neuron(source="anthropic", model="claude-opus-4-6"),
)

# OpenAI-compatible hosted endpoints  -  groq, openrouter, together, mistral.
# Pre-configured huggingface Neurons; api_key falls back to <PROVIDER>_API_KEY.
axon = Axon(
    neuron_id="fast",
    neuron_fn=Neuron(source="groq", model="llama-3.1-70b-versatile"),
)
```

`Neuron(...)` returns an async callable satisfying `NeuronFn`. Pass `prompt` or
`messages` (OpenAI-style) in the task input. Output is always
`{"response": "<text>", "meta": <raw provider payload>}`.

| `source=` | Kind | Key kwargs |
|---|---|---|
| `"ollama"` | LLM | `model`*, `endpoint`, `system`, `temperature`, `max_tokens` |
| `"huggingface"` / `"hf"` | LLM | `endpoint`*, `model`, `use_chat_api`, `api_key`, `max_new_tokens` |
| `"openai"` | LLM | `model`*, `api_key` (or `OPENAI_API_KEY`), `endpoint`, `temperature`, `max_tokens`, `system` |
| `"anthropic"` | LLM | `model`*, `api_key` (or `ANTHROPIC_API_KEY`), `system`, `max_tokens`, `temperature` |
| `"groq"` / `"openrouter"` / `"together"` / `"mistral"` | LLM | `model`, `api_key` (or `<PROVIDER>_API_KEY`), `endpoint`, `temperature`, `max_new_tokens` |
| `"mcp"` | MCP server | `command`+`args` *or* `server`+`args`, `env`, `cwd`, `tool` |

Requires `httpx` (`pip install httpx`).

---

## Synapse adapters

| Import | Use when |
|---|---|
| `MemorySynapse` | tests, single-process |
| `DevSynapse` | multi-process dev on one host (`cosmo synapse start memory`) |
| `NatsSynapse` | production default (`pip install cosmonapse[nats]`) |
| `KafkaSynapse` | durable audit log (`pip install "cosmonapse[kafka]"`) |

URL factory:

```python
from cosmonapse import connect_synapse, synapse_from_url

synapse = await connect_synapse("cosmo://127.0.0.1:7070")   # DevSynapse
synapse = await connect_synapse("nats://nats:4222")          # NatsSynapse
synapse = await connect_synapse("kafka://broker:9092")       # KafkaSynapse
```

For `MemorySynapse`, construct directly:

```python
from cosmonapse import MemorySynapse
synapse = MemorySynapse()
await synapse.connect()
```

---

## Storage backends

| Import | Use when |
|---|---|
| `MemoryRegistryStore` | tests, ephemeral orchestrators |
| `SqliteRegistryStore` | single-process production, zero extra 