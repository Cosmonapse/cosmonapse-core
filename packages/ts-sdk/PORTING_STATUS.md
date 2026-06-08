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
- Clarification & permission requests: the `__clarification__` / `__permission__`
  return-markers (Axon emits `CLARIFICATION` / `PERMISSION`), the new
  `PERMISSION` / `PERMISSION_DECISION` / `CLARIFICATION_ANSWER` signal types and
  builders, the `onPermission` handler, and the responder helpers
  `respondToPermission` / `grantPermission` / `denyPermission` /
  `answerClarification`. At parity with the Python SDK. There is no blocking
  "cognition client" in either SDK  -  the return-marker carries it. (Engram-
  backed recall of prior answers exists in the Python SDK only for now; see
  "Still to port".)
- Cognition & coordination signal builders: `planSignal`, `thoughtDeltaSignal`,
  `toolCallSignal`, `toolResultSignal`, `escalationSignal`, `consensusSignal`,
  `contextSyncSignal`, `discoverSignal` (and the `DISCOVER` `SignalType`). At
  parity with the Python `cosmonapse.envelope` builders.
- Engram value layer (`engram.ts`, `engram-client.ts`): the `RECALL` /
  `RECALLED` / `IMPRINT` / `IMPRINTED` signal types + builders + `newEngramId`;
  the `Engram` contract, `EngramBinding`, `Hit` / `RecallResult` /
  `ImprintReceipt`, the exception family, the default `InMemoryEngram` (full
  recall/imprint incl. merge/upsert/delete + idempotency + deep-merge), and the
  caller-side `EngramClient` (deadline + parent_id correlation, decoupled from
  the Dendrite via an `EngramPublisher` interface). Mirrors the Python
  `cosmonapse.engram` base/memory/client. Only the Dendrite/Axon wiring is
  still outstanding (see below).
- Engram persistent backends (`engram-sqlite.ts`, `engram-postgres.ts`):
  `SqliteEngram` (optional `better-sqlite3`) and `PostgresEngram` (optional
  `pg`), both implementing the same `Engram` contract as `InMemoryEngram`,
  ported from `cosmonapse.engram.sqlite` / `.postgres`. Lazy-imported like the
  registry stores. Typecheck-verified; exercise against a real DB in CI before
  relying on them (the in-sandbox port could not run native `better-sqlite3` /
  a live Postgres).
- Neuron sources: `mcpNeuron`, `ollamaNeuron`, `huggingFaceNeuron`, and the
  unified `neuron()` factory. (The Express / HTTP-app Neuron was removed  -  an
  HTTP API is not a Neuron; front an orchestrator Dendrite with your web
  framework instead.)
- Hosted-LLM provider neurons: `openaiNeuron`, `anthropicNeuron`, and the
  OpenAI-compatible aliases `groq` / `openrouter` / `together` / `mistral`
  via `neuron(source, opts)` (`neuron-openai.ts`). Uses the runtime `fetch`;
  keys resolve from `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / provider env
  vars. At parity with the Python `Neuron(source=...)` registry.
- Source-paired Axon factories + recognition (`axon.ts`). At parity with the
  Python additions:
  - `Axon.fromSource(source, neuronId, opts, extra?)` plus `Axon.openai` /
    `ollama` / `huggingface` / `anthropic` / `mcp` static factories that pair a
    `neuron(source, ...)` with its recogniser.
  - `outputParser` option + the `__error__` marker (yields ERROR without
    throwing); `errorResult()` / `isErrorOutput()` in `neuron.ts`.
  - `parseLlmIntents` / `parseMcpIntents` recognisers (the `{"cosmo": ...}`
    intent convention; MCP `is_error` -> ERROR).
  - The decorator model `detectsOutput` / `detectsClarification` /
    `detectsPermission` / `detectsError` (the asking side; named apart from the
    Dendrite's `on*` inbound handlers), applied in precedence
    error -> clarification -> permission -> output. Sync or async detectors.

## Still to port (tracked, not yet implemented)

| Area | Gap | Python reference | Notes |
| --- | --- | --- | --- |
| Engram Dendrite/Axon wiring | The hosting + caller integration is not wired into `Dendrite` / `Axon`. | Python `Dendrite.attach_engram` / `detach_engram`, RECALL/IMPRINT subscription + dispatch, RECALLED/IMPRINTED delivery to `EngramClient`, terminal-event `cancel_trace`; `Axon(engrams=[...])` binding whitelist + `recall`/`imprint` helper injection. | `EngramClient` already exists and only needs an `EngramPublisher` (the Dendrite). **Design decision required:** Python injects `recall`/`imprint` into the Neuron fn by introspecting parameter names (`inspect`), which TS cannot do — pass a context object to the Neuron instead. Integration-heavy; do it against a working local build. |

## Known intentional differences (not gaps)

- **Clarification / permission marker helpers.** The TS SDK provides typed
  marker constructors and guards  -  `clarify()` / `isClarification()` and
  `permissionRequest()` / `isPermissionRequest()`  -  whereas Python Neurons
  return the raw `{"__clarification__": True, ...}` / `{"__permission__": True,
  ...}` dicts. Same wire result; the TS helpers exist because the type system
  makes them cheap and ergonomic.
- **Optional-dependency drivers differ by ecosystem.** The Python SDK uses
  stdlib `sqlite3`, `asyncpg`, `aiokafka`, and `httpx`; the TS SDK uses
  `better-sqlite3`, `pg`, `kafkajs`, and the runtime's built-in `fetch`
  (Node 18+). These are the idiomatic Node equivalents and are lazy-imported as
  optional dependencies, so the core package installs without any of them.
- **`NeuronRecord` timestamps are RFC 3339 strings** in the