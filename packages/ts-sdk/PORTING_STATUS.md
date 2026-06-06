# TypeScript SDK  -  porting status

This file is the single source of truth for what the TypeScript SDK
(`@cosmonapse/sdk`) has ported from the reference Python SDK (`cosmonapse`) and
what is still outstanding. It replaces the scattered "Still to port…" comments
that used to live inline in the source  -  a developer reading a source file
should not have to guess whether a gap is known or tracked. It is.

When you open or close one of these items, update this file (and ideally mirror
it to a GitHub Issue). When you finish a port, move the row to **Ported** and
delete the corresponding caveat comment from the source.

## Ported and functional

- Envelope types, codec, and validation (`envelope.ts`)
- Typed signal builders (`signals.ts`)
- `Synapse` interface + in-process `MemorySynapse` (`synapse.ts`)
- `NatsSynapse` networked adapter (`synapse-nats.ts`)
- `DevSynapse` + `DevSynapseServer` local dev broker (`synapse-dev.ts`)
- `KafkaSynapse` networked adapter (`synapse-kafka.ts`, optional `kafkajs`)
- `synapseFromUrl` / `connectSynapse` URL factory (`url.ts`)
- `RegistryStore` interface + `MemoryRegistryStore` (`storage.ts`),
  `SqliteRegistryStore` (`storage-sqlite.ts`, optional `better-sqlite3`),
  `PostgresRegistryStore` (`storage-postgres.ts`, optional `pg`)
- `LifecycleHooks`  -  `onConnect` / `onRefresh` / `onSchedule` (`hooks.ts`),
  wired into `Axon` and `Dendrite`
- `Neuron` contract + `Axon` + `Dendrite` (incl. the registry mirror)
- Neuron sources: `mcpNeuron`, `ollamaNeuron`, `huggingFaceNeuron`, and the
  unified `neuron()` factory. (The Express / HTTP-app Neuron was removed  -  an
  HTTP API is not a Neuron; front an orchestrator Dendrite with your web
  framework instead.)
- Hosted-LLM provider neurons: `openaiNeuron`, `anthropicNeuron`, and the
  OpenAI-compatible aliases `groq` / `openrouter` / `together` / `mistral`
  via `neuron(source, opts)` (`neuron-openai.ts`). Uses the runtime `fetch`;
  keys resolve from `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / provider env
  vars. At parity with the Python `Neuron(source=...)` registry.

## Still to port (tracked, not yet implemented)

Nothing outstanding  -  the LLM provider neurons added to the Python SDK in
0.1.1 were ported to TypeScript in the same release. New gaps, if any are
discovered, should be added here.

| Area | Gap | Python reference | Notes |
| --- | --- | --- | --- |
|  -  |  -  |  -  | All previously-tracked gaps are ported. |

## Known intentional differences (not gaps)

- **Optional-dependency drivers differ by ecosystem.** The Python SDK uses
  stdlib `sqlite3`, `asyncpg`, `aiokafka`, and `httpx`; the TS SDK uses
  `better-sqlite3`, `pg`, `kafkajs`, and the runtime's built-in `fetch`
  (Node 18+). These are the idiomatic Node equivalents and are lazy-imported as
  optional dependencies, so the core package installs without any of them.
- **`NeuronRecord` timestamps are RFC 3339 strings** in the