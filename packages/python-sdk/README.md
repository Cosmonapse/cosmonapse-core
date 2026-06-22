# cosmonapse

Event-driven Agent-to-Agent protocol SDK for Python.

**v0.1.0**  -  Python 3.11+ · MIT

---

## Install

```bash
pip install cosmonapse

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

## Clarification & permission

A Neuron can pause and ask instead of returning a result, by returning a marker.
The Axon turns it into a `CLARIFICATION` or `PERMISSION` signal; an answering
Dendrite (a central Cortex or any peer) replies. No extra client  -  the Engram
is the memory, the return-marker is the request.

```python
async def writer(input, context, *, recall, imprint):
    # 1. Try the Engram of standing grants first.
    if input.get("permission"):                      # resumed with a verdict
        if input["permission"]["granted"]:
            await imprint("grants", op="upsert", merge_key="action",
                          entry={"action": "write_file", "granted": True})
            return {"wrote": True}
        return {"wrote": False}
    hit = await recall("grants", query={"action": "write_file"})
    if hit.hits:
        return {"wrote": True}                        # already allowed
    # 2. Miss -> ask. Same shape as {"__clarification__": True, ...}.
    return {"__permission__": True, "action": "write_file",
            "scope": {"path": "/tmp/out"}, "reason": "persist output"}

# Answering side (centralised or decentralised  -  just who subscribes):
@cortex.on_permission
async def decide(sig):
    granted = sig.payload["action"] == "write_file"
    # Re-dispatch a TASK carrying the verdict so the Neuron resumes:
    await cortex.respond_to_permission(sig, granted=granted, reason="allowlisted")
    # ...or emit a discrete signal instead:
    # await cortex.grant_permission(sig, reason="allowlisted", ttl_ms=3_600_000)
```

`on_clarification` / `respond_to_clarification` / `answer_clarification` are the
clarification-side equivalents. See `design/ENVELOPE_SPEC.md` §7.5.

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
| `NatsSynapse` | production default (`pip install cosmonapse`) |
| `KafkaSynapse` | durable audit log (`pip install cosmonapse`) |

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

A `RegistryStore` is the orchestrator's view of the live Neuron population,
rebuilt from `REGISTER` / `HEARTBEAT` / `DEREGISTER` Signals. Pass one as
`registry_store=` when an orchestrator dispatches by capability instead of by
`neuron_id`.

| Import | Use when |
|---|---|
| `MemoryRegistryStore` | tests, ephemeral orchestrators |
| `SqliteRegistryStore` | single-process production, zero extra deps |
| `PostgresRegistryStore` | multi-process / multi-host (`pip install cosmonapse`) |

---

## Pathway

`dendrite.dispatch(...)` returns a `Pathway` - a per-trace event handle with
three consumption shapes on the same primitive:

```python
# 1. Sequential request/reply - block for the first terminal Signal.
sig = await orch.dispatch_and_wait(neuron="answerer", input={"q": "hi"}, timeout_s=5.0)
print(sig.payload["output"])

# 2. Reactive - trace-scoped callbacks.
pw = await orch.dispatch_and_subscribe(capabilities=["plan"], input={"goal": "..."})

@pw.on(SignalType.THOUGHT_DELTA)
async def stream(s):
    print(s.payload["delta"], end="")

# 3. Streaming iteration.
async with await orch.dispatch(capabilities=["plan"], input={"goal": "..."}) as pw:
    async for sig in pw:
        if sig.type is SignalType.AGENT_OUTPUT:
            break
```

`Pathway(scope=...)` filters delivery. `"all"` (default) sees every
`PATHWAY_TYPES` Signal on the trace; `"terminal"` delivers only
FINAL / ERROR / CLARIFICATION / PERMISSION - the decentralised pattern where
intermediate orchestration is handled peer-to-peer and the Cortex only wakes
for a conclusion or a blocked decision. Pathways auto-close on FINAL or ERROR.
`observe_pathway(trace_id)` opens a Pathway in observer role for a trace another
peer started. See the `04-pathway` example.

---

## Engram (shared memory)

An `Engram` is a memory backend addressed over the bus. The Neuron stays pure -
it gains keyword-only `recall` / `imprint` parameters that the Axon injects
because the Axon was constructed with `engrams=[EngramBinding(...)]`. Under the
hood `recall(...)` emits a `RECALL` Signal on the current trace and awaits the
matching `RECALLED` reply.

```python
from cosmonapse import Axon, Dendrite, EngramBinding, InMemoryEngram

async def researcher(input, context, *, recall, imprint):
    prior = await recall("ctx", query={"text": input["question"]})
    if prior.hits:
        return {"answer": prior.hits[0].entry["answer"], "source": "cache"}
    answer = solve(input["question"])
    await imprint("ctx", op="upsert", merge_key=f"q:{input['question']}",
                  entry={"question": input["question"], "answer": answer})
    return {"answer": answer, "source": "computed"}

# Host owns the backend; engram_id is the wire address.
host.attach_engram(InMemoryEngram(engram_id="ctx", engram_kind="context"))

# Worker binds a local name to that wire id, so ops can repoint the
# backend without editing Neuron code.
worker.attach_axon(Axon(
    neuron_id="researcher", neuron_fn=researcher, capabilities=["research"],
    engrams=[EngramBinding(name="ctx", directed_id="ctx")],
))
```

Backends: `InMemoryEngram` (tests), `SqliteEngram` (single-host durable),
`PostgresEngram` (`pip install cosmonapse`). See the
`06-engram-integration` and `11-rag` examples.

---

## The cosmo CLI

`pip install cosmonapse` also installs the `cosmo` command:

| Command | What it does |
|---|---|
| `cosmo init [name]` | Scaffold a runnable Axon + Dendrite project (`-n` namespace, `--force`) |
| `cosmo synapse start memory --namespace=demo` | Boot a local TCP+NDJSON dev broker at `cosmo://127.0.0.1:7070` |
| `cosmo dispatch` | Dispatch a TASK from the terminal and await the reply |
| `cosmo registry` | Inspect the live Neuron population (DISCOVER-based) |
| `cosmo answer` | Interactively answer CLARIFICATION / PERMISSION requests |
| `cosmo doppler -n demo` | Stream every Signal on the bus to stdout (`--type` to filter, `--prism` for the browser view) |
| `cosmo schema` | Export the Signal envelope JSON Schema |
| `cosmo validate` | Validate that Signals on the bus conform to the envelope spec |
| `cosmo completion` | Print a shell-completion script (bash / zsh / fish) |

Watch any workflow live:

```bash
cosmo synapse start memory --namespace=demo
cosmo doppler --prism --url=cosmo://127.0.0.1:7070 -n demo   # opens http://127.0.0.1:7071
```

---

## Examples

The companion `cosmonapse-examples` repo has 13 runnable, numbered examples -
from a single-process hello-world up through capability routing, competitive
bidding, a full hybrid-retrieval RAG system, an MCP-backed coding agent, and
retry / STOP / saga-rollback. Start with `01-quickstart`.

---

## License

MIT (c) 2026 Aqib Khan
