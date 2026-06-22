# Cosmonapse SDK  -  Design Document

**Status:** Draft v0.2
**Audience:** Cosmonapse contributors, early adopters, protocol implementers
**Last updated:** 2026-05-21

---

## 1. Purpose

This document specifies the developer-facing surface of the Cosmonapse SDK and the runtime contracts the SDK depends on. It is the canonical reference for how applications, agents, and tools interact with the Cosmonapse distributed cognition fabric.

The SDK is the ergonomic skin over the **Cosmonapse Protocol**. Anything an application can do can also be done by speaking the protocol directly  -  the SDK exists to make that pleasant.

### 1.1 Goals

- Provide a small, opinionated API for building autonomous agent fabrics.
- Make intent dispatch, lifecycle handshakes, and inbound Signal handling *first-class primitives*, not afterthoughts bolted onto generic pub/sub.
- Be synapse-agnostic. The application should not know whether the runtime is backed by NATS, Kafka, or a local dev TCP broker.
- Be model-agnostic. The SDK does not know or care which LLM is producing thought.
- Make peer-to-peer and centralised orchestration equally first-class.
- Match the Python and TypeScript surfaces 1:1 wherever the language allows.

### 1.2 Non-goals

- The SDK is **not** a prompt framework.
- It is **not** a workflow DAG engine. Workflows emerge from components publishing cognitive events; the SDK exposes primitives, not a declarative graph language.
- It does not ship an LLM inference runtime  -  Neurons bring their own.
- It does not bake in an orchestration model  -  both centralised (one Cortex) and decentralised (many cooperating Dendrites) are supported with the same primitives.

---

## 2. Mental model

A Cosmonapse system is a population of **Neurons** (agents) exchanging **Signals** about **Tasks**, observed by a **Doppler**, persisted through a **RegistryStore**, optionally orchestrated by a **Cortex**.

The five concrete primitives the developer touches:

| Primitive       | What it is                                            | Lifetime     |
|---|---|---|
| `Neuron`        | A pure async function  -  the agent itself. Zero protocol knowledge. Optionally created via `Neuron(source=...)` provider factories (Ollama, HuggingFace). | Process |
| `Axon`          | Agent-side tool that turns Neuron output into Signals | Process      |
| `Dendrite`      | Synapse-side participant. Only `synapse` is required; `registry_store`, Axons, inbound handlers, and heartbeat are all opt-in. Orchestration primitives (`dispatch_task`, `emit_final`, `on_agent_output`, …) live here too. | Process |
| `RegistryStore` | Persistent view of Neurons seen on a namespace (optional)     | Process+     |
| `Synapse`       | Synapse adapter (memory / dev / NATS / Kafka). Caller-owned. | Process      |

(`Cortex` is kept as a back-compat alias for `Dendrite`  -  there is no separate orchestrator class.)

Everything else in the SDK is a convenience over these five.

### 2.1 Vocabulary

| Term       | Maps to                  | Definition                                                                            |
|---|---|---|
| **Brain**  | Team of agents           | Collection of Neurons sharing a Synapse                                               |
| **Neuron** | Agent                    | A single agent  -  pure function, zero protocol knowledge                               |
| **Axon**   | Skill / connector / tool | Agent-side tool that validates the Neuron's output into a Signal                      |
| **Dendrite** | Synapse-side process   | Connects to the Synapse; hosts Axons; owns pub/sub, REGISTER, HEARTBEAT, DEREGISTER   |
| **Cortex** | Orchestrating Dendrite   | Back-compat alias for `Dendrite`. A Dendrite that uses `dispatch_task` / `on_agent_output` / etc. is colloquially a "Cortex"  -  no separate class exists. |
| **Synapse** | Channel / stream        | The synapse layer all Signals cross                                                 |
| **Signal** | Envelope                 | A single message crossing the Synapse                                                 |
| **Engram** | Context / memory         | Persistent shared state (per-Task input + the developer's own helpers)                |
| **Doppler** | Watcher                  | Passive, read-only listener on the Synapse                                            |
| **RegistryStore** | Local DB           | The one mandatory persistent surface: live view of Neurons (capabilities, status, heartbeat) |

### 2.2 The shape of a workflow

A typical end-to-end flow (centralised case):

1. A Cortex calls `await cortex.dispatch_task(neuron=..., input=...)`.
2. The TASK envelope is published on the Synapse.
3. The Dendrite hosting the addressed Neuron's Axon receives the TASK.
4. The Dendrite invokes the Axon's `handle_task(task)`. The Axon resolves any `context_ref`, calls the Neuron function, and returns an `AGENT_OUTPUT` / `CLARIFICATION` / `PERMISSION` / `ERROR` Signal.
5. The Dendrite publishes the returned Signal on the Synapse.
6. The Cortex's `on_agent_output` handler runs and decides what comes next  -  emit FINAL, dispatch another TASK, etc.

Every step is just Signals crossing the Synapse. The SDK only hides the choreography.

### 2.3 Decentralised flow

There may be no Cortex at all. Several Dendrites can coexist on the same namespace, each using lifecycle hooks (§5) to:

- announce themselves on connect (`on_connect`),
- reconcile peer state periodically (`on_refresh`),
- run domain-specific periodic work (`on_schedule(every_s=...)`),
- handle inbound Signals via `dendrite.subscribe(SignalType.X, handler)`.

The Cortex remains an option, not a requirement.

---

## 3. Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                       Application code                           │
│           (Neurons, workflows, dashboards, supervisors)          │
└──────────────────────────────────────────────────────────────────┘
                                │
        ┌───────────────────────┼────────────────────────────┐
        ▼                       ▼                            ▼
┌──────────────┐        ┌──────────────────────────────────────────┐
│    Axon      │  ←──→  │              Dendrite                    │
│  (agent-     │        │  (synapse-side participant;               │
│  side tool)  │        │   also the "Cortex" when using           │
└──────────────┘        │   dispatch_task / on_agent_output / etc.)│
                        └──────────────┬───────────────────────────┘
                                       │
                                       ▼
                               ┌───────────────┐
                               │ RegistryStore │
                               │  (optional)   │
                               └───────────────┘
                                       │
                                       ▼
                                ┌────────────┐
                                │  Synapse   │
                                └────────────┘
                                       │
               ┌───────────────────────┼──────────────────────────┐
               ▼                       ▼                          ▼
         MemorySynapse           DevSynapse                 NatsSynapse
                          (cosmo synapse start memory)      KafkaSynapse
```

### 3.1 The agent-as-tool boundary

The Neuron itself has zero protocol knowledge. The Axon is the only piece of Cosmonapse that lives inside the Neuron's process. In v1 the Axon is an in-process Python helper; in v2 it ships as an MCP server, so an arbitrary LLM-driven agent can talk to a remote Dendrite over HTTP without ever importing Python from Cosmonapse.

The Dendrite is the only thing that touches the Synapse. Everything that crosses the wire  -  REGISTER, HEARTBEAT, DEREGISTER, TASK routing, AGENT_OUTPUT, CLARIFICATION, PERMISSION, ERROR  -  is the Dendrite's responsibility. The Axon hands the Dendrite an already-valid Signal and is done.

`Cortex` is a back-compat alias for `Dendrite`  -  there is no separate subclass. A Dendrite that calls `dispatch_task` and registers `on_agent_output` handlers is effectively a Cortex. It is still just another participant on the Synapse  -  there is nothing privileged about it from the protocol's point of view.

---

## 4. Python SDK surface

### 4.1 Axon  -  agent-side tool

```python
from cosmonapse import Axon

async def answerer(input, context):
    return {"answer": input["q"]}

axon = Axon(
    neuron_id="answerer",
    neuron_fn=answerer,
    capabilities=["text"],
    version="0.0.1",
)
```

The Axon owns the Neuron's identity (`neuron_id`, `capabilities`, `version`) and the tool body (`neuron_fn`). It does not touch the Synapse  -  it must be attached to a Dendrite to participate.

### 4.2 Dendrite  -  synapse-side connector

`synapse` is the only required parameter. Every other capability is opt-in.

```python
from cosmonapse import Dendrite, SqliteRegistryStore, connect_synapse

synapse = await connect_synapse("cosmo://127.0.0.1:7070")

# Worker Dendrite  -  no registry_store needed just to host an Axon
worker = Dendrite(synapse=synapse, namespace="prod")
worker.attach_axon(axon)

# Orchestrator Dendrite  -  pass registry_store to enable registry helpers
orch = Dendrite(
    synapse=synapse,
    registry_store=SqliteRegistryStore("/tmp/orch.db"),
    namespace="prod",
)

async with worker, orch:
    ...
```

Constructor parameters:

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `synapse` | `Synapse` |  -  | **Required.** Caller builds and closes it. |
| `registry_store` | `RegistryStore \| None` | `None` | Optional. Enables `find_neurons` / `registry_snapshot` and mirrors REGISTER / HEARTBEAT / DEREGISTER from the bus into the store. |
| `namespace` | `str` | `"default"` | Synapse namespace all Signals are published under. |
| `dendrite_id` | `str` | `"dendrite"` | Identifier embedded in outbound FINAL / ERROR signals as `neuron`. |
| `heartbeat_s` | `float` | `30.0` | Per-Axon heartbeat interval in seconds. Pass `0` to disable. |
| `reregister_on_heartbeat` | `bool` | `True` | Re-emit REGISTER on every heartbeat so late-joining consumers discover Axons without a separate sync. Set `False` to emit REGISTER only once at startup. |

The Dendrite handles REGISTER on start, HEARTBEAT on the configured interval, DEREGISTER on stop, routes inbound TASKs to the matching Axon, and publishes the Axon's reply Signal.

**Orchestration methods**  -  available on every Dendrite (no separate Cortex class needed):

| Method | Emits / observes |
|---|---|
| `dispatch_task(neuron=..., input=..., context_ref=..., ...)` | TASK |
| `emit_final(trace_id, parent_id, result)` | FINAL |
| `emit_error(trace_id, parent_id, code, message)` | ERROR |
| `emit(signal)` | refuses anything outside `SYNAPSE_TYPES` |
| `find_neurons(capability=...)` / `registry_snapshot(...)` | reads from RegistryStore (requires `registry_store`) |
| `on_agent_output(fn)` / `on_clarification(fn)` / `on_permission(fn)` / `on_error(fn)` | inbound AXON-type handlers |
| `respond_to_clarification(sig, answer=...)` / `respond_to_permission(sig, granted=...)` | re-dispatch a TASK carrying the answer/verdict so the Neuron resumes |
| `answer_clarification(...)` / `grant_permission(...)` / `deny_permission(...)` | emit a discrete `CLARIFICATION_ANSWER` / `PERMISSION_DECISION` reply |
| `on_register(fn)` / `on_deregister(fn)` / `on_heartbeat(fn)` | inbound lifecycle handlers |

The canonical method names are `on_error_signal`, `on_register_signal`, `on_deregister_signal`, `on_heartbeat_signal`; the shorter forms above are convenient aliases.

Lifecycle hooks (`on_connect`, `on_refresh`, `on_schedule`) are mixed in from `LifecycleHooks`  -  see §5.

### 4.3 Cortex  -  back-compat alias for Dendrite

`Cortex` is exported purely for back-compat. In new code use `Dendrite` directly:

```python
from cosmonapse import Dendrite, MemoryRegistryStore

orch = Dendrite(
    synapse=synapse,
    registry_store=MemoryRegistryStore(),
    namespace="prod",
)

@orch.on_agent_output
async def done(sig):
    await orch.emit_final(trace_id=sig.trace_id, parent_id=sig.id,
                          result=sig.payload["output"])

async with orch:
    await orch.dispatch_task(neuron="answerer", input={"q": "hi"})
    ...
```

### 4.4 Neuron  -  factory for anything that interacts with the real world

A Neuron is any async function satisfying `(input: dict, context: list) -> dict`. It is *not* restricted to an LLM agent  -  a Neuron can be an LLM/agent, an **MCP server** (any stdio MCP server), or a plain async function. The SDK ships optional source wrappers so each can be dropped into any Axon with no boilerplate; the protocol routes to all of them identically:

> An **HTTP API is not a Neuron.** The Flask/WSGI/`api` source (and the TS `expressNeuron`) were removed: a web app is an inbound request handler, not an `input -> output` worker. Keep your framework on the outside as an HTTP boundary and dispatch TASKs from its route handlers via an orchestrator Dendrite instead (see the `neuron_real_world` example).

```python
from cosmonapse import Axon, Neuron

# Local Ollama daemon
axon = Axon(
    neuron_id="chat",
    neuron_fn=Neuron(source="ollama", model="llama3"),
)

# HuggingFace TGI / vLLM / llama.cpp / LM Studio  (OpenAI-compatible)
axon = Axon(
    neuron_id="summariser",
    neuron_fn=Neuron(source="huggingface", endpoint="http://localhost:8080"),
)

# HuggingFace hosted endpoint
axon = Axon(
    neuron_id="classifier",
    neuron_fn=Neuron(
        source="huggingface",
        endpoint="https://<your-endpoint>.endpoints.huggingface.cloud",
        api_key="hf_…",
        use_chat_api=True,
    ),
)
```

`Neuron(...)` returns a `_BaseNeuron` instance that satisfies `NeuronFn`, so it slots directly into `Axon.neuron_fn`.

**Available sources:**

| `source=` | Kwargs | Notes |
|---|---|---|
| `"ollama"` | `model` *(required)*, `endpoint`, `system`, `temperature`, `max_tokens`, `timeout` | LLM. Wraps `/api/generate` (plain prompt) and `/api/chat` (messages). Needs `httpx`. |
| `"huggingface"` / `"hf"` | `endpoint` *(required)*, `model`, `use_chat_api`, `temperature`, `max_new_tokens`, `api_key`, `timeout` | LLM. Uses `/generate` (native TGI) or `/v1/chat/completions` (OpenAI compat). Needs `httpx`. |
| `"mcp"` | `command`+`args` **or** `server`+`args`, plus `env`, `cwd`, `tool` | MCP server. Spawns any stdio MCP server and exposes its tools; input is `{tool, arguments}` (or `{"__list_tools__": True}`). Returns `{response, result, is_error, content, meta}`. Wrapper only  -  does not implement a server. Needs `mcp`. Standard launch presets in `STANDARD_MCP_SERVERS`. |

**TypeScript:** the same sources are available as `mcpNeuron(opts)`, `ollamaNeuron(opts)`, `huggingFaceNeuron(opts)`, plus a unified `neuron(source, opts)` dispatcher and `standardMcpServers`. The MCP client uses `@modelcontextprotocol/sdk` (an optional peer dependency, imported lazily). (There is no `expressNeuron`  -  an HTTP API is not a Neuron.)

**Input dict keys** (passed via `dispatch_task(input=...)`):

- `prompt` (str)  -  plain-text single-turn.
- `messages` (list)  -  OpenAI-style `[{"role": "user", "content": "…"}]` for multi-turn.

**Output**  -  always `{"response": "<generated text>", "meta": <raw provider payload>}`.

**Soft dependency**  -  `httpx` must be installed (`pip install httpx`). It is intentionally not in the core SDK requirements so projects that bring their own Neuron functions don't pull in an extra dependency.

### 4.5 RegistryStore  -  the optional persistent surface

```python
from cosmonapse import RegistryStore, MemoryRegistryStore, SqliteRegistryStore, PostgresRegistryStore
```

| Backend                  | Use when                                        |
|---|---|
| `MemoryRegistryStore`    | tests, ephemeral orchestrators                  |
| `SqliteRegistryStore`    | single-process production, zero extra deps      |
| `PostgresRegistryStore`  | multi-process production (asyncpg, lazy import) |

A third-party backend (Redis, DynamoDB, anything) is conformant iff it passes the `tests/test_registry_store.py` suite.

Anything beyond the registry  -  costs, latency histograms, audit history  -  is the developer's to build by subscribing to the Synapse and persisting whatever they need. The SDK deliberately stops at `RegistryStore` so the mandatory surface stays small.

### 4.6 Synapse  -  Synapse adapters

```python
from cosmonapse import MemorySynapse, DevSynapse, NatsSynapse, KafkaSynapse
```

| Adapter        | Process boundary       | Notes                                                        |
|---|---|---|
| `MemorySynapse` | single process        | tests, tightly-coupled callers                               |
| `DevSynapse`    | local host, many procs | client side of `cosmo synapse start memory` (TCP + NDJSON)  |
| `NatsSynapse`   | cluster               | production default; native wildcards/queue groups            |
| `KafkaSynapse`  | cluster               | durable audit log; trickier request/reply                    |

URL factories  -  `synapse_from_url` builds without connecting; `connect_synapse` builds and connects in one call (recommended):

```python
from cosmonapse import connect_synapse, synapse_from_url

# Build and connect in one step (preferred)
synapse = await connect_synapse("cosmo://localhost:7070")
synapse = await connect_synapse("nats://nats:4222")
synapse = await connect_synapse("kafka://broker:9092")

# Build only (caller must call .connect() separately)
t = synapse_from_url("cosmo://localhost:7070")
await t.connect()
```

For `MemorySynapse`, construct directly  -  there is no URL scheme for in-process transport:

```python
synapse = MemorySynapse()
await synapse.connect()
```

The Dendrite does **not** own the Synapse. The caller builds it, passes it to one or more Dendrites, and closes it when done:

```python
synapse = await connect_synapse("cosmo://127.0.0.1:7070")
try:
    async with Dendrite(synapse=synapse, ...) as d:
        ...
finally:
    await synapse.close()
```

### 4.7 The cosmo CLI

The `cosmo` CLI ships with the Python SDK (`pip install cosmonapse`).

**Synapse management**

```bash
# Boot a local dev synapse (DevSynapseServer  -  TCP + NDJSON, single-host only)
cosmo synapse start memory --namespace=dev

# Boot with NATS or Kafka as the underlying transport
cosmo synapse start nats  --namespace=prod --broker=nats://localhost:4222
cosmo synapse start kafka --namespace=prod --broker=localhost:9092

# List all active namespaces on a running server
cosmo synapse view --url=cosmo://127.0.0.1:7070

# Stream Signals live for one namespace
cosmo synapse view --url=cosmo://127.0.0.1:7070 --namespace=dev

# Gracefully stop a namespace
cosmo synapse stop --url=cosmo://127.0.0.1:7070 --namespace=dev
```

Connect a Dendrite to the running server:

```python
synapse = await connect_synapse("cosmo://127.0.0.1:7070")
```

`DevSynapse` wire protocol: `hello / pub / sub / unsub / msg / err` (newline-delimited JSON). Subject matching follows MemorySynapse / NATS conventions (`*` one token, `>` rest). Queue groups, fan-out, and Doppler subscribers all work identically to the in-memory adapter. Zero external deps  -  dev-only, single host.

**Doppler (passive watcher)**

```bash
cosmo doppler --url=cosmo://127.0.0.1:7070 --namespace=dev
```

**Envelope validation**

```bash
cosmo validate --url=cosmo://127.0.0.1:7070 --namespace=dev
```

---

## 5. Lifecycle hooks

Every primitive (`Axon`, `Dendrite`, `Cortex`) mixes in `LifecycleHooks`, providing three decorators:

| Hook                       | Fires when                                                                                       |
|---|---|
| `on_connect(fn)`           | Once, after this component finishes its own connect handshake (Axon attached + REGISTER emitted; Dendrite/Cortex synapse up + subscriptions wired). |
| `on_refresh(fn)`           | Whenever internal state observably changes  -  heartbeat tick, REGISTER / DEREGISTER / HEARTBEAT observed by a Cortex's registry, or a manual `await component.refresh(reason=..., extra=...)`. The handler receives a `RefreshEvent(reason, neuron_id, extra)`. |
| `on_schedule(every_s=N)(fn)` | Developer-supplied periodic task. Runs as a background coroutine every `every_s` seconds for the lifetime of the component. |

### 5.1 Why these three

`on_connect` and `on_refresh` exist because Cosmonapse explicitly supports **decentralised** operation. With no central orchestrator, every Dendrite needs:

- a moment to announce itself to peers (handshake)  -  `on_connect`,
- a way to react to state changes on the bus (reconciliation)  -  `on_refresh`,
- a way to run periodic gossip / heartbeat / state-refresh tasks  -  `on_schedule`.

These hooks are the substrate on which peer-to-peer fabrics are built.

### 5.2 Examples

```python
# Centralised: log when the Cortex comes up
@cortex.on_connect
async def hello(c):
    snap = await c.registry_snapshot()
    print(f"Cortex up on {c.namespace}: {len(snap)} known neurons")

# Decentralised: a Dendrite reacts to peer registry changes
@dendrite.on_refresh
async def reconcile(d, event):
    if event.reason == "register":
        log.info("Saw new peer: %s", event.neuron_id)

# Periodic: rebuild a derived view every 30s
@cortex.on_schedule(every_s=30)
async def rollup(c):
    snap = await c.registry_snapshot(include_deregistered=True)
    publish_derived_view(snap)

# Per-Axon: emit a custom warm-up Signal on attach
@my_axon.on_connect
async def warmup(a):
    await preload_model_weights()
```

### 5.3 Manual triggers

`await component.refresh(reason="manual", extra={...})` fires `on_refresh` with the supplied event. Useful when the developer's own code knows state has changed but the SDK can't detect it (e.g. a custom side-channel update).

---

## 6. The Synapse contract (Synapse interface)

```python
class Synapse(Protocol):
    async def connect(self) -> None: ...
    async def close(self) -> None: ...
    async def publish(self, subject: str, signal: Signal) -> None: ...
    async def subscribe(self, subject: str, handler, *, queue_group: str | None) -> Subscription: ...
    async def request(self, subject: str, signal: Signal, *, timeout_s: float) -> Signal: ...
```

Subject convention: `cosmonapse.<namespace>.<TYPE>`. Wildcards: `*` (one token), `>` (rest). Queue groups load-balance round-robin within a group; subscribers with no queue_group form the Doppler population (every matching message goes to each one).

Adapters are responsible for ordering, dedup window, ack semantics, and durability. The SDK assumes "at-least-once delivery, in-order per key" and dedups in the envelope codec.

---

## 7. The Signal envelope

See **ENVELOPE_SPEC.md** for the full spec. Summary:

| Type           | Producer  | Notes                                                                |
|---|---|---|
| `TASK`         | Cortex    | unit of work addressed to a neuron                                   |
| `AGENT_OUTPUT` | Dendrite  | wraps the Axon's raw output                                          |
| `CLARIFICATION` | Dendrite | Neuron returned a `__clarification__` marker                        |
| `PERMISSION`   | Dendrite  | Neuron returned a `__permission__` marker (asks before acting)      |
| `CLARIFICATION_ANSWER` / `PERMISSION_DECISION` | Dendrite | answer / verdict to a request; `parent_id` = the request's id |
| `ERROR`        | Dendrite / Cortex | exception during handling, or orchestration-level failure    |
| `FINAL`        | Cortex    | workflow terminated successfully                                     |
| `REGISTER` / `DEREGISTER` / `HEARTBEAT` | Dendrite | per-Axon lifecycle                          |
| `TASK_OFFER` / `BID` / `TASK_AWARDED` / `TASK_DECLINED` | Cortex | optional bid-based routing |
| `THOUGHT_DELTA` / `PLAN` / `TOOL_CALL` / `TOOL_RESULT` / `MEMORY_APPEND` / `CRITIQUE` / `ESCALATION` / `CONSENSUS` / `CONTEXT_SYNC` | Cortex | optional cognition-style envelopes for richer workflows |

The Cortex refuses to emit any Signal whose type isn't in `SYNAPSE_TYPES`; the Axon only ever returns `AGENT_OUTPUT / CLARIFICATION / PERMISSION / ERROR`. This is enforced in code, not just convention.

### 7.1 Clarification & permission: ask-and-resume, no extra client

`CLARIFICATION` and `PERMISSION` are *requests* a Neuron raises by returning a marker  -  `{"__clarification__": True, ...}` or `{"__permission__": True, "action": ...}`  -  rather than a normal result. The Axon turns the marker into the matching Signal; the Dendrite publishes it. A Neuron typically tries an Engram `RECALL` first and only returns the marker on a miss.

The answering side is a `Dendrite` with an `on_clarification` / `on_permission` handler  -  a central Cortex or any peer (centralised vs decentralised is just *who subscribes*). It can:

- **re-dispatch a TASK** carrying the answer/verdict via `respond_to_clarification` / `respond_to_permission` (preserving `trace_id`, `parent_id` = the request id) so the Neuron resumes and can `IMPRINT` the decision into an Engram for next time; or
- **emit a discrete reply** (`CLARIFICATION_ANSWER` / `PERMISSION_DECISION`) for a peer or observer to consume.

There is deliberately **no blocking correlation client**: the Engram is the durable memory and the return-marker is the request channel, so no extra per-Dendrite state is introduced (see DECISIONS.md).

---

## 8. Versioning

Envelope `v="1"`. SemVer-major changes are protocol-breaking; minor additions (new types, new optional fields) are wire-compatible. The SDK supports current major + previous one for one minor release.

---

## 9. Out of scope (v0.2)

- Federation across namespaces / clusters.
- Wallet / chargeback semantics beyond cost annotation.
- WebAssembly synapse for in-browser Neurons.
- Built-in fine-tuning data export.

The TypeScript SDK is **shipping at v0.2** with full envelope/codec, all signal builders, `MemorySynapse`, `NatsSynapse`, in-memory `RegistryStore`, `Neuron`/`Axon`/`Dendrite` ports. Still to port in a future release: `KafkaSynapse`, `SqliteRegistryStore` / `PostgresRegistryStore`, and provider-backed `Neuron` factories.

---

## 10. Roadmap

**v0.2 (this release)**  -  Axon / Dendrite / Cortex alias; Neuron provider factories (Ollama, HuggingFace); RegistryStore + memory/sqlite/postgres; memory/dev/NATS/Kafka synapses; `cosmo synapse start|view|stop`, `cosmo doppler`, `cosmo validate`; lifecycle hooks; `connect_synapse` URL helper. TypeScript SDK: envelope, builders, MemorySynapse, NatsSynapse, RegistryStore mirror, Axon, Dendrite, Neuron contract.

**v0.3**  -  Axon as MCP server; `cosmo dev cortex` and `cosmo dev dendrite` scaffold subcommands; durable REGISTER replay on join; TypeScript: KafkaSynapse, SqliteRegistryStore/PostgresRegistryStore, provider-backed Neuron factories.

**v0.4**  -  declarative router DSL on top of Dendrite primitives.

**v0.5**  -  a Neuron-driven Dendrite (the orchestrator is itself an 