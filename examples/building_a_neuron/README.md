# Building a Neuron

**Difficulty:** Beginner · **Primitives:** Neuron, Axon, Dendrite, Synapse, Pathway

The smallest possible Cosmonapse program with a real LLM. One Neuron backed by
Hugging Face, one Axon, one Dendrite, one TASK, one reply. Single process,
in-memory Synapse, no broker to start.

This is the example to read first. Every other example in this directory adds
something on top of this shape — and notice that the LLM didn't add any
boilerplate. The Axon attaches to `Neuron(source="huggingface", ...)` exactly
the same way it would attach to a hand-written async function.

## Setup

```bash
pip install cosmonapse httpx
export HF_TOKEN=hf_xxxxxxxxxxxxxxxxxxxxxxxx   # read scope is enough
python examples/building_a_neuron/main.py
```

Get a token at <https://huggingface.co/settings/tokens>. Read access is all you
need — the call goes to the public Inference Providers router.

Expected output:

```
[AGENT_OUTPUT] Hello, Cosmonapse! Welcome aboard — let's build something cool.
```

(Exact text varies — the model is stochastic.)

## What it shows

| Layer | What it is |
|---|---|
| **Neuron** | `Neuron(source="huggingface", endpoint=..., model=..., api_key=...)` returns an async callable that satisfies the Neuron contract. Same shape as a hand-written function. |
| **Axon** | Wraps the Neuron, declares `neuron_id` + `capabilities`. Doesn't know or care that it's an LLM. |
| **Dendrite** | The only component that touches the Synapse. Hosts Axons, routes inbound TASKs, exposes `dispatch_*`. |
| **Synapse** | The message transport. `MemorySynapse` here; swap for NATS / Kafka / DevSynapse in production. |
| **Pathway** | What `dispatch_and_wait` is built on — a per-trace handle that resolves on the first terminal Signal. |

## Swap the model

The endpoint is the only thing that's HF-specific. Point it elsewhere to use
any OpenAI-compatible chat server:

```python
# Inference Providers router (default in the example)
endpoint="https://router.huggingface.co"

# Dedicated Hugging Face Inference Endpoint
endpoint="https://<your-endpoint>.endpoints.huggingface.cloud"

# Local TGI / vLLM / llama.cpp / LM Studio
endpoint="http://localhost:8080"
```

For local Ollama, use `source="ollama"` instead:

```python
neuron_fn = Neuron(source="ollama", model="llama3")
```

## Where to go next

- [`engram_integration/`](../engram_integration/) — bind shared memory to an Axon and recall / imprint from inside a Neuron.
- [`quickstart/`](../quickstart/) — the same program split across worker / orchestrator processes against the dev TCP Synapse.
- [`neuron_providers/`](../neuron_providers/) — every Neuron source in one place (Ollama, HF, Flask, MCP).
