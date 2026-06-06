# Cosmonapse  -  Design Decisions

**Status:** Living document
**Last updated:** 2026-05-17

Record of every significant design decision made during the Cosmonapse architecture phase, with the reasoning behind each one.

---

## 1. What Cosmonapse is

**Decision:** Cosmonapse is a protocol spec + SDK + CLI. It is not a hosted platform, not a workflow engine, and not a router-as-a-service.

**Rationale:** Developers need full control over orchestration logic. Shipping a hosted platform or a pre-built router would bake in assumptions about how workflows should work. The SDK gives developers the primitives to build whatever system they need, with the protocol as the only shared contract.

**What this means in practice:**
- Cosmonapse ships five things only: envelope spec, Axon/Dendrite/Cortex primitives, RegistryStore, synapse adapters, `cosmo` CLI.
- Developers build their own workflows, routers, and team configurations on top of the primitives.
- Any two components that produce valid Signals can interoperate  -  that is the only guarantee Cosmonapse makes.

---

## 2. The components Cosmonapse ships

| What                       | Why                                                                                       |
|---|---|
| **Envelope spec**          | The single shared contract.                                                              |
| **Axon**                   | Makes any Neuron a protocol participant without the Neuron knowing the protocol exists. |
| **Dendrite**               | Owns the connection to the Synapse and the per-Axon lifecycle (REGISTER / HEARTBEAT / DEREGISTER + TASK routing). |
| **Cortex**                 | A Dendrite plus the minimum primitives to act as an orchestrator (dispatch_task / emit_final / inbound handlers / registry). |
| **RegistryStore**          | The one mandatory persistent surface; namespaces' live Neuron view.                      |
| **Synapse adapters**     | Memory, dev-synapse (TCP), NATS, Kafka.                                                  |
| **`cosmo` CLI**            | `cosmo dev synapse` for local dev; `cosmo doppler`, `cosmo validate` for tooling.        |

Nothing else.

---

## 3. Neuron contract

**Decision:** A Neuron is *anything that interacts with the real world*, exposed behind a pure-function interface  -  `async fn(input, context) -> output`. It has zero knowledge of the protocol, envelopes, trace IDs, or workflow semantics. A Neuron is therefore not restricted to "an LLM agent": it can be an LLM/agent, an **MCP server** (any stdio MCP server, wrapped as a tool surface), or any plain async function. The `Neuron(source=...)` factory turns each of these into the same `NeuronFn` callable.

An **HTTP API is deliberately *not* a Neuron.** Earlier versions shipped a Flask/WSGI/`api` source (and a TS `expressNeuron`) that replayed each TASK as an in-process HTTP request; this was removed. A web app is the wrong shape for a worker  -  it is an inbound request handler, not an outbound `input -> output` mapping, and wrapping one inverted the natural control flow. The supported pattern is the reverse: keep your web framework (Flask, FastAPI, Express, …) on the **outside** as an HTTP boundary and dispatch TASK Signals from inside its route handlers via an orchestrator Dendrite, wiring `@dendrite.on_agent_output` directly in the app. See the `neuron_real_world` example.

**Rationale:** The protocol's only contract with a worker is the `NeuronFn` signature. As long as a thing can map a TASK's `input` to a JSON-serialisable result, it can be a Neuron  -  so the same Axon/Dendrite machinery routes to an LLM, a microservice, or an MCP server interchangeably, and the rest of the system can't tell them apart. Neurons should be replaceable without touching infrastructure; keeping them behind a pure-function interface means any existing agent **or service** can be wrapped with an Axon and become a protocol participant with no modification, and stays fully testable in isolation.

**Source wrappers shipped:** `ollama`, `huggingface`/`hf` (LLM, over httpx); `mcp` (any stdio MCP server, over the `mcp` client SDK  -  a wrapper around existing servers, not a new server). Each is a soft dependency, pulled in only when that source is used. The `mcp` source also ships `STANDARD_MCP_SERVERS` launch presets (filesystem, fetch, git, memory, …) for well-known published servers. (There is no Flask/WSGI/`api` source  -  an HTTP API is not a Neuron; see above.)

**What the Neuron receives:**
- `input`  -  the `payload.input` object from the TASK envelope. Arbitrary JSON.
- `context`  -  fetched by the Axon using `payload.context_ref`. Empty list if no context was provided.

**What the Neuron returns:**
- A JSON-serialisable object. No envelope, no type, no trace ID.
- Or a `{"__clarification__": True, "question": ..., "context": ...}` dict to request more info.
- Or a `{"__permission__": True, "action": ..., "scope": ..., "reason": ...}` dict to ask permission before acting (typically only after an Engram `recall` misses).

---

## 4. Axon contract

**Decision:** The Axon is the agent-side tool that turns Neuron output into a protocol-valid Signal. It does **not** touch the Synapse.

**The Axon's complete output surface (Signals it returns from handle_task):**

| Signal              | When                                            |
|---|---|
| `AGENT_OUTPUT`      | Neuron returned a normal result                 |
| `CLARIFICATION`     | Neuron's output contained the `__clarification__` marker |
| `PERMISSION`        | Neuron's output contained the `__permission__` marker |
| `ERROR`             | Neuron raised an exception                      |

The Axon never produces `REGISTER`, `HEARTBEAT`, `DEREGISTER`, `FINAL`, `THOUGHT_DELTA`, or any routing Signal. Those belong to the Dendrite and the Cortex.

**Rationale:** Decoupling Signal *content* from Signal *transmission* lets the Axon ship as a small in-process helper today and as an MCP server in v0.3  -  the agent never knows the difference because the Axon's interface to the agent stays identical.

---

## 5. Dendrite contract (the only synapse-side class)

**Decision:** There is exactly one synapse-side class  -  `Dendrite`. Its only required argument is `synapse`. Every behaviour beyond bare pub/sub is opt-in:

- attach an Axon → enables TASK routing + lifecycle Signals for it,
- register a handler decorator → enables the inbound subscription for that Signal type,
- pass a `registry_store` → enables the bus-driven registry mirror + auto-subscription to REGISTER/DEREGISTER/HEARTBEAT,
- set `heartbeat_s=0` → disables the heartbeat loop entirely.

The Dendrite always exposes the orchestration surface  -  `dispatch_task`, `emit_final`, `emit_error`, `emit`, `on_agent_output`, `on_clarification`, `on_permission`, `respond_to_clarification`, `respond_to_permission`, etc.  -  regardless of whether you use it.

**The Dendrite does NOT own the Synapse.** The caller builds it via `await connect_synapse(url)` (or directly), passes it in, and closes it. Multiple Dendrites can share one Synapse.

**Lifecycle Signal emit surface (when Axons are attached):**

| Signal      | When                                                                                  |
|---|---|
| `REGISTER`  | On start, and re-emitted on each HEARTBEAT tick (for late-joining peers)             |
| `HEARTBEAT` | Periodic, configurable interval                                                       |
| `DEREGISTER` | On stop                                                                              |
| The Axon's `AGENT_OUTPUT` / `CLARIFICATION` / `PERMISSION` / `ERROR` reply | After invoking `axon.handle_task(task)` |

**Rationale:** A "Cortex" was just "a Dendrite with a few subscriptions and convenience methods"  -  making it a separate class added a concept without earning it. Decentralised fabrics in particular benefit from a single symmetric primitive where every peer can take on any role.

---

## 6. Cortex is a back-compat alias

**Decision:** `Cortex` is kept as a plain alias of `Dendrite` (`Cortex = Dendrite`). No separate class and no extra constructor keywords. Use `dendrite_id=` for the identifier.

**Rationale:** Existing code that imports `Cortex` keeps working without rewrites; new code uses `Dendrite` directly.

---

## 7. Only `synapse` is required at construction

**Decision:** `Dendrite(synapse=...)` is the minimal valid construction. `registry_store`, axons, handlers, and `heartbeat_s>0` are all opt-in. `find_neurons()` and `registry_snapshot()` raise `RuntimeError` if no store was provided.

**Rationale:** Forces zero ceremony for the simplest case (a Dendrite that just publishes a few Signals) while still enabling the full registry-tracked orchestrator with one extra argument. Was previously two required arguments + auto-wired subscriptions  -  both turned out to be more than the protocol actually needs.

## 8. URL-based `connect()`

**Decision:** `await Dendrite.connect(url, registry_store=...)` and `await Cortex.connect(url, registry_store=...)` build the right Synapse from the URL scheme, connect it, and return a started component. Schemes: `cosmo://` (dev), `nats://`, `kafka://`. `MemorySynapse` is constructed directly because a URL would be ambiguous across processes.

**Rationale:** The CLI prints `cosmo://host:port`, so the natural code is `await Cortex.connect("cosmo://host:port", ...)`. Symmetric for NATS and Kafka. The synapse is then owned by the component and is closed automatically on `stop()`.

---

## 9. Lifecycle hooks: on_connect / on_refresh / on_schedule

**Decision:** Every primitive (Axon, Dendrite, Cortex) exposes three hook decorators via the shared `LifecycleHooks` mixin:

- `on_connect(fn)`  -  fire-once after the component completes its own connect handshake.
- `on_refresh(fn)`  -  fired whenever observable state changes (heartbeat tick, registry mutation, manual `await component.refresh(...)`). Receives a `RefreshEvent(reason, neuron_id, extra)`.
- `on_schedule(every_s=N)(fn)`  -  periodic background task.

**Rationale:** Cosmonapse explicitly supports both centralised (one Cortex up first, then workers) and **decentralised** (many Dendrites, no Cortex) operation. Decentralised peer fabrics need:

- a handshake moment to announce themselves on the bus,
- a way to react to peer state changes,
- a way to run periodic gossip/state-sync.

These three hooks cover all three needs without baking in a particular discovery protocol. A developer building a decentralised swarm wires their own gossip into `on_schedule`; a developer using a centralised Cortex can use `on_connect` to log readiness and ignore the rest.

---

## 10. RegistryStore is the only mandatory persistent surface

**Decision:** The SDK ships exactly one Store interface: `RegistryStore`. Backends: `MemoryRegistryStore`, `SqliteRegistryStore`, `PostgresRegistryStore`. For costs, latency, audit history, etc., the developer subscribes to the Synapse and writes their own helpers.

**Rationale:** The temptation to ship `CostStore`, `HeartbeatStore`, `TraceStore`, `LatencyStore` is real and wrong. Each one we add becomes an API surface we have to maintain forever. The registry is unavoidable  -  capability-based routing depends on it  -  but everything else is a wide-open design space where developer-specific schemas are better than ours. The SDK exposes the raw envelope stream; the developer indexes it however they need.

---

## 11. `cosmo dev synapse` over TCP NDJSON

**Decision:** Local dev synapse is a stdlib-only TCP server speaking newline-delimited JSON. Wire ops: `hello / pub / sub / unsub / msg / err`. Subjects, wildcards, queue groups, request/reply all match MemorySynapse / NATS semantics.

**Rationale:** A WebSocket / gRPC dev broker would force a dep just to test cross-process behaviour. TCP + NDJSON is ~400 lines of stdlib, runs on every platform, and gives the multi-process dev experience without any infra setup. It is explicitly **not** a production synapse  -  that's NATS or Kafka.

---

## 12. CLARIFICATION and PERMISSION are Axon-produced markers

**Decision:** The Axon inspects the Neuron's output before wrapping it. If it detects the `__clarification__` marker it returns CLARIFICATION; if it detects the `__permission__` marker it returns PERMISSION; the Dendrite publishes either directly. The Cortex receives a typed envelope and never needs to inspect AGENT_OUTPUT payloads.

**Rationale:** Detecting these at the agent boundary keeps the Cortex's handler dispatch typed and predictable. The conventions (`__clarification__: True` / `__permission__: True` markers) are the agreed contract between Neuron developer and Axon. PERMISSION is just CLARIFICATION specialised for a boolean verdict.

---

## 12a. No blocking cognition client  -  clarification/permission ride return-markers + Engram

**Decision:** Clarification and permission do **not** get a dedicated caller-side correlation client (an earlier "CognitionClient" prototype was removed). The request is the return-marker above; the answer comes back one of two ways, both built on primitives that already exist:

1. **Re-dispatch a TASK** with the answer/verdict via `respond_to_clarification` / `respond_to_permission` (preserves `trace_id`, sets `parent_id` to the request id). The Neuron resumes and can `IMPRINT` the decision into an Engram.
2. **Emit a discrete reply** signal (`CLARIFICATION_ANSWER` / `PERMISSION_DECISION`, `parent_id` = request id) for a peer or observer to consume.

The new signal *types* (`PERMISSION`, `PERMISSION_DECISION`, `CLARIFICATION_ANSWER`) ship; the *transport* is the developer's to wire. The canonical pattern: a Neuron `recall`s an Engram of standing grants/answers, returns the marker only on a miss, and the answering Dendrite imprints the decision so future recalls hit  -  centralised (one Cortex) or decentralised (any peer subscribes) with no code difference.

**Rationale:** A blocking correlation client duplicated the Engram round-trip machinery (RECALL/RECALLED) and added per-Dendrite state and lifecycle (deadlines, cancel-on-terminal) for no capability the Engram + return-marker don't already provide. The Engram is the durable memory; the bus is the transport; keeping both as the only moving parts holds the "Dendrite is the only thing that touches the Synapse, and it stays thin" invariant.

---

## 13. The Doppler

**Decision:** The Doppler is not an SDK primitive. It is just a process that subscribes to the Synapse using the Synapse's `subscribe` method without a `queue_group`. Cosmonapse ships `cosmo doppler` as the built-in Doppler; developers build their own visualisations the same way.

**Passive constraint:** A Doppler process must never publish to the channel. All adapters connect non-queue subscribers as non-competing consumer groups so they cannot block delivery to Dendrites or Cortices.

---

## 14. Synapse abstraction

**Decision:** All signal routing is abstracted behind a narrow interface: `connect`, `publish`, `subscribe`, `request`, `close`. Adapters shipped: `MemorySynapse`, `DevSynapse`, `NatsSynapse`, `KafkaSynapse`. Optional client libraries (`nats-py`, `aiokafka`) are lazy-imported.

**Rationale:** Same code paths for tests, local dev, and production. The conformance test suite for `MemorySynapse` defines correct behaviour; any new adapter passes the same suite.

---

## 15. Build approach

**Decision:** Python SDK first, TypeScript SDK later. Monorepo. In-memory synapse before any real synapse.

**Build order (delivered in v0.2):**

1. Envelope types + codec (Pydantic).
2. In-memory synapse.
3. Axon (agent-side, narrow).
4. Dendrite (synapse-side, hosts Axons).
5. Cortex (Dendrite + orchestration).
6. RegistryStore + memory / sqlite / postgres backends.
7. NATS + Kafka synapses (lazy-imported).
8. DevSynapseServer + DevSynapse + `cosmo dev synapse`.
9. LifecycleHooks (on_connect / on_refresh / on_schedule).

**Post-v0.2:**

10. Axon-as-MCP server (replacing in-process attach for remote agents).
11. TypeScript SDK.
12. `cosmo dev cortex` / `cosmo dev dendrite` scaffold subcommands.
13. Declarative router DSL on top of Cortex.

**"First five minutes" rule:** A developer must be able to `pip install cosmonapse`, write 20 lines, and see it work with no external infrastructure. Both `MemorySynapse` and `cosmo dev synapse` exist to keep that rule honest.

**Test suite = conformance suite:** Tests for `MemorySynapse` define correct adapter behaviour. Tests for `MemoryRegistryStore` define correct store behaviour. Third-party adapters and backends run the same suites to verify conformance.

---

## 16. Version roadmap

**v0.1.0 (current):** First public release. Manual SDK  -  developer reads the spec
and builds Dendrites by hand. Ships the Python SDK (reference implementation), a
preview TypeScript SDK, the `cosmo` CLI, Engram shared memory, Pathways,
capability-routed dispatch, and competitive bidding. Full control, full
complexity, appropriate for early adopters. The detailed, milestone-by-milestone
path from here to **1.0.0** (stabilisation: CI, machine-readable schema, broker
integration tests, TS parity) lives in [`ROADMAP.md`](./ROADMAP.md).

**Post-1.0 direction (indicative, not committed):**

- *Axon as installable MCP server.* Agents on EC2 / inside Claude / inside Cursor
  wired in without any Python dependency.
- *Declarative router.* Higher-level config compiles to an orchestrator Dendrite.
  The manual surface remains available.
- *Router-as-Neuron.* A Cosmonapse agent that builds and tunes routers from the
  Doppler stream. Only possible because the protocol is self-describing.

---

## 17. Things deliberately excluded from the 0.x line

The TypeScript SDK was previously listed here as excluded ("post Python
stabilisation"). It now ships as a preview alongside the Python SDK in 0.1.0;
its remaining parity gaps are tracked in
[`packages/ts-sdk/PORTING_STATUS.md`](./packages/ts-sdk/PORTING_STATUS.md), not
here.

| Excluded                          | Reason                                            |
|---|---|
| Hosted platform / cloud control plane | Adds operational complexity before the protocol is proven |
| Reference router implementation   | Would bake in routing assumptions the developer should own |
| Federation across namespaces      | Post-1.0                                          |
| Billing / chargeback beyond cost annotation | Post-1.0                               |
| GUI for the Doppler               | Developer's own visualisation  -  not Cosmonapse's job |
| `CostStore` / `LatencyStore` / etc. | Developer-specific schemas; the SDK exposes the raw envelope stream and stops there |

---

## 18. Terminology

All internal and external naming uses these terms.

| Term       | Maps to                  | Definition                                                                |
|---|---|---|
| **Brain**  | Team of agents           | Collection of Neurons sharing a Synapse                                   |
| **Neuron** | Agent                    | Pure function, zero protocol knowledge                                    |
| **Axon**   | Agent-side tool          | Validates Neuron output into Signals                                      |
| **Dendrite** | Synapse-side process   | Hosts Axons, owns pub/sub + lifecycle Signals                            |
| **Cortex** | (alias of Dendrite)      | Back-compat alias; new code uses Dendrite directly                       |
| **Synapse** | Channel / stream        | The synapse layer                                                       |
| **Signal** | Envelope                 | A single message crossing the Synapse                                     |
| **Engram** | Context / memory         | Persistent shared state read via context_ref                              |
| **Doppler** | Watcher / observer      | Passive read-only listener                                                |
| **RegistryStore** | Local DB           | Live view of Neurons (capabilities, status, heartbeat)                    |
| **LifecycleHooks** | Mixin             | 