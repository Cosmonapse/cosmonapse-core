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

## Still to port (tracked, not yet implemented)

Nothing outstanding from the original parity gap list  -  every row below has
been closed. New gaps, if any are discovered, should be added here.

| Area | Gap | Python reference | Notes |
| --- | --- | --- | --- |
|  -  |  -  |  -  | All previously-tracked gaps are ported. |

## Known intentional differences (not gaps)

- **Optional-dependency drivers differ by ecosystem.** The Python SDK uses
  stdlib `sqlite3`, `asyncpg`, `aiokafka`, and `httpx`; the TS SDK uses
  `better-sqlite3`, `pg`, `kafkajs`, and the runtime's built-in `fetch`
  (Node 18+). These are the idiomatic Node equivalents and are lazy-imported as
  optional dependencies, so the core package installs without any of them.
- **`NeuronRecord` timestamps are RFC 3339 strings** in the TS store interface
  (`last_heartbeat`, `registered_at`), where Python uses `datetime`. This is the
  existing `storage.ts` contract and is preserved by the sqlite/postgres ports.

- **`NeuronRecord` field naming is snake_case** (`neuron_id`, `last_heartbeat`,
  `registered_at`). This is deliberate: the record is the on-the-wire / on-disk
  shape shared with the Python SDK and must round-trip verba