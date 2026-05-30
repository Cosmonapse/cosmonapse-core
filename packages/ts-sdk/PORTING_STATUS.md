# TypeScript SDK — porting status

This file is the single source of truth for what the TypeScript SDK
(`@cosmonapse/sdk`) has ported from the reference Python SDK (`cosmonapse`) and
what is still outstanding. It replaces the scattered "Still to port…" comments
that used to live inline in the source — a developer reading a source file
should not have to guess whether a gap is known or tracked. It is.

When you open or close one of these items, update this file (and ideally mirror
it to a GitHub Issue). When you finish a port, move the row to **Ported** and
delete the corresponding caveat comment from the source.

## Ported and functional

- Envelope types, codec, and validation (`envelope.ts`)
- Typed signal builders (`signals.ts`)
- `Synapse` interface + in-process `MemorySynapse` (`synapse.ts`)
- `NatsSynapse` networked adapter (`synapse-nats.ts`)
- `RegistryStore` interface + in-memory `MemoryRegistryStore` (`storage.ts`)
- `Neuron` contract + `Axon` + `Dendrite` (incl. the registry mirror)
- Neuron sources: `expressNeuron`, `mcpNeuron`, and the unified `neuron()` factory

## Still to port (tracked, not yet implemented)

Each item below corresponds to a parity gap against the Python SDK. File a
GitHub Issue per row and link it here.

| Area | Gap | Python reference | Notes |
| --- | --- | --- | --- |
| Local dev | `DevSynapse` / `DevSynapseServer` | `cosmonapse/synapse/dev.py` | Lets TS-first users run a local dev broker from Node instead of booting the Python `cosmo synapse start memory` process. |
| Ergonomics | `connectSynapse(url)` URL factory | `cosmonapse/_url.py` | One-liner that picks an adapter from a URL scheme, instead of `new MemorySynapse()` by hand. |
| Lifecycle hooks | `LifecycleHooks` — `on_connect` / `on_refresh` / `on_schedule` | `cosmonapse/_hooks.py`, mixed into `axon.py` + `dendrite.py` | Decentralised p2p workflows depend on `on_connect` / `on_schedule`. The Axon/Dendrite caveat comments point here. |
| Neuron sources | LLM provider factories — `neuron("ollama", …)`, `neuron("huggingface", …)` | `cosmonapse/neuron.py`, `cosmonapse/_neuron_http.py` | TS currently ships only Express + MCP sources. |
| Transport | `KafkaSynapse` | `cosmonapse/synapse/kafka.py` | Needs an external Kafka client; cannot be fully integration-tested in-process. |
| Storage | `SqliteRegistryStore`, `PostgresRegistryStore` | `cosmonapse/storage/sqlite.py`, `cosmonapse/storage/postgres.py` | Only `MemoryRegistryStore` is ported. Postgres needs an external driver. |

## Known intentional differences (not gaps)

- **`NeuronRecord` field naming is snake_case** (`neuron_id`, `last_heartbeat`,
  `registered_at`). This is deliberate: the record is the on-the-wire / on-disk
  shape shared with the Python SDK and must round-trip verbatim. See the header
  of `src/storage.ts`. A camelCase view/adapter could be layered on top later;
  if added, document it here.
