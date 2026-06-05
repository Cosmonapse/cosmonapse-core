# Examples

Runnable, self-contained programs that exercise the SDK. Each example is one
folder; read it top-to-bottom and you have seen every layer it touches.

Sorted by difficulty  -  start at the top and work down.

## Beginner

### [`building_a_neuron/`](./building_a_neuron/)

The smallest possible Cosmonapse program  -  one Neuron, one Axon, one
Dendrite, one TASK, one reply. Single process, in-memory Synapse, no broker
to start.

**Primitives:** Neuron · Axon · Dendrite · Synapse · Pathway

### [`quickstart/`](./quickstart/)

The same program as `building_a_neuron`, split across four scripts so the
worker and orchestrator run in separate processes against the dev TCP
Synapse (`cosmo synapse start memory`). The shape every multi-process
Cosmonapse system takes.

**Primitives:** Neuron · Axon · Dendrite · Synapse · CLI

## Intermediate

### [`engram_integration/`](./engram_integration/)

Bind shared memory to a Neuron with `EngramBinding`. The Neuron calls
`recall()` and `imprint()` to read and write the bound Engram without ever
touching the protocol. Backed by `InMemoryEngram` here; swap for
`SqliteEngram` / `PostgresEngram` without editing Neuron code.

**Primitives:** Engram · EngramBinding · Axon · Dendrite

### [`neuron_providers/`](./neuron_providers/)

Replace the hand-written Neuron with a provider-backed one. Defaults to
`Neuron(source="huggingface", ...)` against the Inference Providers router,
with `Neuron(source="ollama", ...)` shown as a one-line swap. The Axon never
knows whether the Neuron is a function, an LLM, an HTTP app, or an MCP
server  -  that's the point.

**Primitives:** Neuron sources · Axon · Dendrite

### [`neuron_real_world/`](./neuron_real_world/)

A Neuron is "anything that interacts with the real world"  -  a plain function,
a wrapped stdio MCP server, an LLM. An **HTTP API is not a Neuron**: instead of
wrapping a web app behind an Axon, you keep your framework (Flask / Express) on
the outside as an HTTP boundary and dispatch TASKs from its route handlers via
an orchestrator Dendrite, using the Dendrite's decorators directly in the app.
Includes a TypeScript variant (`express_mcp.ts`) showing the same pattern.

**Primitives:** Neuron sources · Axon · Dendrite (worker + orchestrator roles)

### [`quickstart-hf/`](./quickstart-hf/)

The `quickstart` topology with HuggingFace-backed worker Neurons and a
round-robin orchestrator. Includes a `QUICKSTART.md` walkthrough.

**Primitives:** Neuron(huggingface) · Axon · Dendrite

## Advanced

### [`parallel_build/`](./parallel_build/)

"Build a website"  -  one high-level task fans out across specialised
Neurons that write to a shared Engram. Downstream Neurons read what the
upstream ones wrote. The Cortex coordinates and emits FINAL when complete.

**Primitives:** Engram · capability routing · cognition signals · Dendrite

## Conventions

- Every example is **runnable as written**. No placeholders, no `# TODO:
  fill in your model`.
- Examples that need network or models (Ollama, HuggingFace, NATS, Kafka,
  Postgres) say so in the first 