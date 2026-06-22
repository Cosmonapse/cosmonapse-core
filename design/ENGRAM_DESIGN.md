# Cosmonapse Engram  -  Design (Draft)

**Status:** Draft v0.1
**Last updated:** 2026-05-29
**Relates to:** `ENVELOPE_SPEC.md` §7.4 (Memory), `SDK_DESIGN.md` §2.1 (Engram vocabulary)

---

## 1. What an Engram is

An **Engram** is a storage wrapper. It is the second persistent surface in Cosmonapse after the `RegistryStore`, and it is optional in the same way.

- An Engram wraps **one** backend (sqlite, postgres, a vector store, an object store, anything that can hold bytes and answer queries).
- It exposes a **uniform async interface** for two intents: **recall** (read) and **imprint** (write).
- It listens on the Synapse for `RECALL` / `IMPRINT` Signals and responds with `RECALLED` / `IMPRINTED` Signals.
- It owns its own schema. The protocol does not constrain what an Engram stores, only how it is addressed and how it advertises itself.

An Engram is not a Neuron. It does not produce `AGENT_OUTPUT`. It is a synapse-side participant with its own envelope category  -  the same way a Dendrite is.

A namespace may run **zero, one, or many** Engrams, each serving a **distinct memory purpose**  -  one for working context, one for vectors, one for blobs, one for relational records. The intended default is **addressed routing**: a recall says "I want the vector Engram" (by `engram_id`) or "I want a `semantic` Engram" (by `engram_kind`) and the matching Engram answers. Fan-out across multiple Engrams of the same kind is an opt-in for cases like multi-source retrieval  -  not the default mental model.

This is closer in spirit to how `TASK` routes by `neuron` than to how `TASK_OFFER` auctions over `BID`s. Engrams are addressable singletons in the namespace; bidding is not the goal.

---

## 2. Design principles

- **Recall/imprint are part of the task workflow, not standalone.** A `RECALL` or `IMPRINT` emitted by a Neuron mid-task inherits the containing `TASK.trace_id`. The parent_id chain proves causation. Doppler, cost rollup, deadlines, and any `FINAL` / `ERROR` terminal event apply to the whole slice  -  Engram I/O included.
- **Neurons are the primary issuer.** The expected traffic mix is: a handful of Cortex-level recalls per workflow (pre-task hydration, post-task summarisation), and many Neuron-level recalls/imprints inside each TASK. The Axon's helper API is the hot path. Cortex-level helpers are convenience.
- **Storage is plural.** No single "the memory." Multiple Engrams coexist, one per purpose.
- **Engrams are black boxes.** The protocol sees opaque keys, queries, results. The schema is the Engram's business.
- **Event-driven only.** Engrams never expose a direct method to neurons. Everything is a Signal.
- **Backends are pluggable.** A `SqliteEngram`, `PostgresEngram`, `PgVectorEngram`, `QdrantEngram`, `S3Engram` all conform to the same `Engram` ABC.
- **Idempotent imprint.** Every imprint carries a deterministic `imprint_id`. Replays do not duplicate.
- **Schema-on-write, schema-on-read.** Each Engram declares its `engram_kind` and a JSON Schema for what it accepts/returns. The Cortex routes by capability the same way it routes Tasks.

---

## 3. Layers

```
┌────────────────────────────────────────────────┐
│   Cortex / any Dendrite                        │
│   emits RECALL / IMPRINT                       │
└──────────────────┬─────────────────────────────┘
                   │  Signals on the Synapse
┌──────────────────▼─────────────────────────────┐
│   Engram Dendrite (hosts one or more Engrams)  │
│   subscribes to RECALL with matching kind/caps │
│   subscribes to IMPRINT with matching kind     │
│   emits RECALLED / IMPRINTED                   │
└──────────────────┬─────────────────────────────┘
                   │
┌──────────────────▼─────────────────────────────┐
│   Engram (backend wrapper)                     │
│   recall(query) -> results                     │
│   imprint(op, entry) -> id                     │
└──────────────────┬─────────────────────────────┘
                   │
            ┌──────▼──────┐
            │  Backend    │
            │  (pg/sqlite │
            │  /vector/…) │
            └─────────────┘
```

The Engram class is the analogue of the `Neuron`  -  pure logic, no protocol knowledge. The Engram Dendrite is the analogue of the `Axon` + `Dendrite` pair on the agent side  -  it owns the wire.

---

## 4. New Signal types

These additions are backwards-compatible per envelope §9 (additive). `v` stays `"1"`.

### 4.1 `RECALL`  `[D]`

A search request. Any Dendrite can emit it. Engrams whose `engram_kind` matches and whose declared capabilities cover the query may respond.

```json
{
  "type": "RECALL",
  "payload": {
    "engram_id":     "ctx-default",
    "engram_kind":   "semantic",
    "query":         { "text": "k8s scheduler eviction", "top_k": 5 },
    "filters":       { "tags": ["k8s"], "since": "2026-04-01T00:00:00Z" },
    "context_ref":   "ctx://debug/session-7",
    "deadline_ms":   400,
    "min_confidence": 0.6,
    "recall_mode":   "first"
  }
}
```

| Payload field   | Required | Description |
|---|---|---|
| `engram_id`     | conditional | Explicit target. If present, only that Engram responds. One of `engram_id` or `engram_kind` MUST be set. |
| `engram_kind`   | conditional | Typed routing. Conventional values: `relational`, `semantic`, `keyvalue`, `blob`, `timeseries`, `context`. Engrams subscribe by kind. |
| `query`         | yes | Opaque to the protocol. The Engram interprets it. |
| `filters`       | no  | Optional facet filters. |
| `context_ref`   | no  | Same form as TASK. Scopes the recall (trace / session / project). |
| `deadline_ms`   | no  | Best-effort SLA. Engrams that cannot meet it should not respond. |
| `min_confidence`| no  | Drops weak hits before responding. |
| `recall_mode`   | no  | `"first"` (default  -  one responder wins, others drop the request), `"merge"` (fan-out, caller merges), `"all"` (fan-out, caller gets each `RECALLED` separately). |

**Routing precedence.** `engram_id` beats `engram_kind`. When only `engram_kind` is set and multiple Engrams of that kind exist, behaviour depends on `recall_mode`. The conventional deployment runs one Engram per kind, so `recall_mode: "first"` is a safe default.

### 4.2 `RECALLED`  `[D]`

```json
{
  "type": "RECALLED",
  "payload": {
    "engram_id":  "pgvector-default",
    "hits": [
      { "id": "eng_01JV...", "score": 0.91, "entry": { } },
      { "id": "eng_01JV...", "score": 0.74, "entry": { } }
    ],
    "truncated": false,
    "took_ms":   38
  }
}
```

`parent_id` MUST point to the `RECALL` event. Multiple Engrams may respond  -  the Cortex merges or picks.

### 4.3 `IMPRINT`  `[D]`

```json
{
  "type": "IMPRINT",
  "payload": {
    "engram_id":   "ctx-default",
    "engram_kind": "semantic",
    "op":          "append",
    "entry": {
      "id":      "eng_01JV...",
      "content": "Eviction triggered by memory pressure on node X.",
      "tags":    ["k8s", "eviction"],
      "embed":   true
    },
    "merge_key":   "incident:42"
  }
}
```

| Payload field | Required | Description |
|---|---|---|
| `engram_id`   | conditional | Explicit target. One of `engram_id` or `engram_kind` MUST be set. |
| `engram_kind` | conditional | Routes to matching Engrams. |
| `op`          | yes | One of `add`, `append`, `merge`, `upsert`, `delete`. |
| `entry`       | yes | Opaque body. The Engram validates against its declared schema. |
| `merge_key`   | conditional | Required when `op = merge` or `op = upsert`. |

Imprints are addressed by default (one Engram writes). Broadcast writes  -  e.g. "every semantic Engram should index this"  -  are explicitly opt-in via `meta.broadcast: true`. The protocol does not promise atomicity across multiple receivers.

`op` semantics:

- `add`  -  insert; fail if id exists.
- `append`  -  append to a sequence/log keyed by `merge_key` (or auto-create one).
- `merge`  -  locate by `merge_key`, deep-merge `entry` into the existing record.
- `upsert`  -  replace if `merge_key` matches, otherwise insert.
- `delete`  -  remove by id or `merge_key`.

### 4.4 `IMPRINTED`  `[D]`

```json
{
  "type": "IMPRINTED",
  "payload": {
    "engram_id": "pg-default",
    "op":        "append",
    "id":        "eng_01JV...",
    "version":   3,
    "took_ms":   12
  }
}
```

### 4.5 Lifecycle signals reuse

Engrams piggyback on the existing agent-management signals  -  no new ones needed:

- `REGISTER` with `payload.role = "engram"`, `payload.engram_kind = "semantic"`, plus `capabilities` listing supported query features (`vector_search`, `bm25`, `sql`, `time_range`, `tags`, …).
- `HEARTBEAT` / `DEREGISTER` unchanged.
- `DISCOVER` can filter by `role: "engram"` and `engram_kind`.

This means `RegistryStore` already tracks Engrams  -  no second registry surface required.

### 4.6 New ULID prefix

- `eng_` for Engram entry ids.

### 4.7 Allowed-producer sets

Add `RECALL`, `RECALLED`, `IMPRINT`, `IMPRINTED` to `SYNAPSE_TYPES`. Axons still cannot produce them  -  they go through the hosting Dendrite, same as `MEMORY_APPEND`.

### 4.8 Relation to existing `MEMORY_APPEND` / `CONTEXT_SYNC`

- `MEMORY_APPEND` becomes a **convenience macro** that compiles to `IMPRINT { op: "append" }`. Keep it for back-compat; mark "prefer IMPRINT" in the spec.
- `CONTEXT_SYNC` is unchanged  -  it's a transient broadcast, not a storage op.

---

## 5. Python surface

### 5.1 `Engram` base class

```python
class Engram(ABC):
    engram_id: str
    engram_kind: str                  # "relational" | "semantic" | …
    capabilities: list[str]           # ["vector_search", "tags", "time_range"]
    version: str

    @abstractmethod
    async def connect(self) -> None: ...
    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def recall(self, query: dict, *, filters=None,
                     context_ref=None, deadline_ms=None) -> list[Hit]: ...

    @abstractmethod
    async def imprint(self, op: str, entry: dict, *,
                      merge_key: str | None = None) -> ImprintResult: ...

    # Optional override  -  Engrams that cannot serve a query should return None
    async def can_serve(self, query: dict) -> bool:
        return True
```

### 5.2 Hosting

```python
from cosmonapse import Dendrite, PgVectorEngram

engram = PgVectorEngram(
    engram_id="pgvector-default",
    dsn="postgres://...",
    table="engrams",
    embed_fn=embed_with_voyage,
)

dendrite = Dendrite(synapse=synapse, registry_store=store)
dendrite.attach_engram(engram)   # mirrors Axon attachment
await dendrite.start()
```

`attach_engram` makes the Dendrite:

1. Emit `REGISTER` with `role="engram"`, `engram_kind`, `capabilities`.
2. Subscribe to `RECALL` filtered to `engram_kind` and respond with `RECALLED`.
3. Subscribe to `IMPRINT` filtered to `engram_kind` and respond with `IMPRINTED`.
4. Heartbeat the Engram alongside attached Axons.

### 5.3 Mid-task recall from inside a Neuron

This is the workflow that drives the whole design: a Neuron is processing a TASK, decides it needs prior context (or a vector lookup, or a blob, or anything else), asks for it, and continues. The Neuron stays a pure function  -  it never touches the Synapse. The Axon exposes a small helper, the Dendrite does the wire work.

```python
async def web_research_neuron(input, context, *, recall, imprint):
    # ask the context Engram for related notes
    prior = await recall(engram_id="ctx-default",
                         query={"text": input["question"]},
                         deadline_ms=300)

    # do the work (e.g. call an MCP web-search tool via a sub-TASK)
    answer = await context.tools.web_search(input["question"], prior=prior.hits)

    # write the new finding back
    await imprint(engram_id="ctx-default",
                  op="append",
                  entry={"content": answer.summary,
                         "tags": ["web", input["question"]]},
                  merge_key=f"q:{input['question']}")

    return {"answer": answer.text}
```

What happens on the wire:

```
Cortex → TASK(neuron=web_research)
   │
   ▼
web_research Axon
   │ recall(...)  ───────────► Dendrite emits RECALL(parent_id=TASK.id, engram_id=ctx-default)
   │                            ▼
   │                          ctx-default Engram Dendrite
   │                            ▼
   │ ◄────── RECALLED(parent_id=RECALL.id, hits=[…]) is correlated by the
   │           Axon's pending-recall table and awoken
   │
   │ (Neuron continues, calls MCP web_search via a nested TASK if needed)
   │
   │ imprint(...) ──────────► Dendrite emits IMPRINT(parent_id=TASK.id, engram_id=ctx-default)
   │                            ▼
   │                          ctx-default Engram Dendrite → IMPRINTED
   │
   ▼
AGENT_OUTPUT(parent_id=TASK.id)
```

Key invariants:

- **Axons never publish `RECALL` / `IMPRINT` themselves.** They call helpers; the hosting Dendrite emits the envelope. Same rule as `MEMORY_APPEND` today.
- **Correlation is by `parent_id`.** The Axon's helper keeps a `pending_recalls: dict[event_id, Future]` and resolves the Future when a matching `RECALLED` arrives. Same trick the Cortex uses for `AGENT_OUTPUT`.
- **`recall_mode` decides futures vs streams.**
  - `"first"` → one `RECALLED` resolves the Future.
  - `"merge"` → collect until `deadline_ms`, then resolve with the merged set.
  - `"all"` → returns an async iterator instead of a Future.
- **Deadlines are real.** If no Engram responds inside `deadline_ms`, the helper raises `EngramTimeout`. The Neuron decides whether to proceed without prior context.
- **Imprint is fire-and-forget by default.** The helper returns as soon as the IMPRINT is on the wire. Pass `await_ack=True` to block until `IMPRINTED` arrives.

The producer-allowed sets stay unchanged:

- `AXON_TYPES` does **not** gain `RECALL` / `IMPRINT`. Axons hand off to the Dendrite.
- `SYNAPSE_TYPES` gains all four new types.

### 5.4 Trace and lifecycle propagation

The lifecycle of a TASK includes every RECALL and IMPRINT the Neuron emits while servicing it. They are not separate workflows. Concretely:

**Trace.** Every RECALL / RECALLED / IMPRINT / IMPRINTED emitted on behalf of a TASK carries that TASK's `trace_id`. No new trace_id is minted. The Axon helpers do this automatically  -  the application code never thinks about trace_id.

The SDK enforces this beyond the injected helpers: `Axon.handle_task` binds the TASK's `(trace_id, parent_id = TASK.id)` as an ambient context (a `ContextVar`, async-safe) for the whole handling pass - neuron_fn, detector hooks (`detects_output` & co.), and lifecycle hooks. `dendrite.recall(...)` / `dendrite.imprint(...)` called anywhere inside that pass inherit it when no explicit `trace_id` is given, so e.g. a cache-write imprint fired from a `detects_output` hook lands on the containing TASK's trace. Explicit ids always win; only calls outside any task context (pre-task hydration) mint a fresh trace.

**Causal chain.** Within a single TASK slice:

```
TASK.id   = T
  RECALL.parent_id     = T
    RECALLED.parent_id = R   (R = RECALL.id)
  IMPRINT.parent_id    = T
    IMPRINTED.parent_id = I  (I = IMPRINT.id)
  AGENT_OUTPUT.parent_id = T
```

`parent_id` always points at the immediate causal event. The full causal tree is reconstructable by walking parents; the trace_id gives you the cheap slice query.

**Cost rollup.** `RECALLED` and `IMPRINTED` MAY set `meta.cost_micro_usd` (e.g. embedding fees, vector-db read units). These aggregate into the TASK's total per the existing `cost_micro_usd` convention in §5.4 of the envelope spec. The TASK's `budget_usd`, if set, bounds the whole slice  -  recalls included.

**Deadlines.** A RECALL's `deadline_ms` is local to that recall. The TASK's own `deadline` still bounds the slice as a whole  -  a Neuron that burns its TASK deadline waiting on recalls gets a TASK-level timeout.

**Terminal events.** The first `FINAL` or `ERROR` on `trace_id` wins as before. A late `RECALLED` or `IMPRINTED` arriving after a terminal event is dropped (consumers de-dupe by id and ignore post-FINAL events).

**Cancellation.** If the Cortex cancels a TASK (via `ERROR { kind: "cancelled" }`), the Axon's pending-recall Futures resolve with `EngramCancelled`. In-flight imprints are not rolled back  -  the protocol is at-least-once. Engrams that need exactly-once apply the `imprint_id` dedupe.

**Doppler view.** A Doppler subscribed to `trace_id = T` sees the TASK, every RECALL/RECALLED, every IMPRINT/IMPRINTED, intermediate cognition events, and the AGENT_OUTPUT  -  in one stream. No separate memory log needs to be reconciled.

### 5.5 Who emits what

| Issuer | When | What it emits | Surface |
|---|---|---|---|
| **Neuron (via Axon)** | inside `neuron_fn` while servicing a TASK | most RECALL, most IMPRINT | injected `recall` / `imprint` helpers  -  the hot path |
| **Cortex / orchestrating Dendrite** | before dispatching a TASK (hydrate context); after FINAL (persist summary, memoise result) | a handful per workflow | `dendrite.recall(...)` / `dendrite.imprint(...)` |
| **Engram itself** | when one Engram needs another to satisfy a query (cache fill, projection materialisation) | RECALL → another Engram, then IMPRINT to itself | same helpers on its hosting Dendrite |
| **Doppler / external observer** | never |  -  | read-only |

The Axon's helper is therefore the surface that needs the most polish: deterministic correlation, bounded queues, deadline enforcement, clean cancellation. The Cortex's helpers can wrap it.

### 5.6 Axon wiring of available Engrams

Because Neurons issue most of the traffic, the SDK should let the developer declare which Engrams a Neuron may talk to at construction time. This keeps the Neuron a pure function and makes the dependency graph inspectable.

```python
axon = Axon(
    neuron_id="web_research",
    neuron_fn=web_research_neuron,
    capabilities=["web", "search"],
    engrams=[
        EngramBinding(name="ctx",     engram_id="ctx-default"),
        EngramBinding(name="vectors", engram_kind="semantic"),
    ],
    version="0.0.1",
)
```

At call time the Axon injects `recall` / `imprint` helpers pre-bound to those names:

```python
async def web_research_neuron(input, context, *, recall, imprint):
    prior   = await recall.ctx(query={"text": input["q"]})
    related = await recall.vectors(query={"text": input["q"], "top_k": 5})
    ...
    await imprint.ctx(op="append", entry={...})
```

Two benefits:

- The Neuron cannot accidentally hit an Engram the developer did not whitelist. The Axon refuses.
- Dashboards and `cosmo doppler` can show "Neuron X depends on Engrams [ctx, vectors]" without static analysis of source.

### 5.7 Traffic implications

If every TASK triggers two or three recalls, Synapse traffic scales as `O(TASKs × recalls/TASK)`. For NATS this is fine; for `MemorySynapse` / `DevSynapse` it is also fine; for Kafka, keep RECALL / RECALLED on a low-retention topic separate from TASK / AGENT_OUTPUT.

Two coalescing options the SDK can offer later (not in v1):

- **Per-TASK recall batch**  -  Axon helper collects multiple `recall(...)` calls inside one async tick and emits a single RECALL with a `queries: [...]` payload. The Engram responds once.
- **Per-Engram local cache**  -  Axon caches recent RECALLED results keyed by `(engram_id, query_hash)` for the duration of the TASK. Imprints on the same Engram invalidate it.

Both are SDK-level optimisations; neither changes the wire protocol.

### 5.8 Axon ↔ Dendrite contract for memory

The existing layering invariant holds: **the Axon never touches the Synapse, the Dendrite owns the wire.** Memory does not break this.

When `attach_axon(axon)` runs, the Dendrite injects a tiny in-process `EngramClient` into the Axon. That client is what the helpers (`recall.ctx`, `imprint.vectors`, …) call. The Neuron and the Axon do not import `Signal`, `SignalType`, or any envelope code.

```python
class EngramClient(Protocol):
    async def recall(
        self,
        *,
        binding: EngramBinding,      # resolved from the Axon's declared engrams
        query: dict,
        filters: dict | None = None,
        deadline_ms: int | None = None,
        recall_mode: str = "first",
        # injected by the Dendrite at call time:
        trace_id: str,
        parent_id: str,              # the TASK's id (or current causal parent)
    ) -> RecallResult: ...

    async def imprint(
        self,
        *,
        binding: EngramBinding,
        op: str,                     # "add" | "append" | "merge" | "upsert" | "delete"
        entry: dict,
        merge_key: str | None = None,
        await_ack: bool = False,
        trace_id: str,
        parent_id: str,
    ) -> ImprintReceipt: ...
```

What the Axon hands the Dendrite, per call:

| Field | Source |
|---|---|
| `binding` | from the Axon's declared `engrams=[...]` list  -  resolves to `engram_id` or `engram_kind` |
| `query` / `entry` / `op` / `merge_key` / `filters` | from the Neuron's call |
| `deadline_ms` / `recall_mode` / `await_ack` | defaults from the binding, overridable per call |

What the Dendrite does:

1. **Build the envelope.** Stamps `v`, generates `id`, copies `trace_id` from the active TASK context, sets `parent_id`, fills `payload` from the binding + call args.
2. **Register the pending Future.** `pending_recalls[envelope.id] = Future()` (or `pending_imprints` if `await_ack=True`).
3. **Publish.** On the Synapse, subject derived from envelope type.
4. **Correlate.** A single subscription on RECALLED / IMPRINTED resolves the matching Future by `parent_id`.
5. **Enforce.** Deadline timer cancels the Future with `EngramTimeout`. A `FINAL`/`ERROR` on the trace cancels in-flight Futures with `EngramCancelled`.
6. **Return.** The helper returns to the Neuron with the result, or raises.

The Axon's job collapses to two things:

- At construction time: validate that every `EngramBinding` resolves and expose typed accessors (`recall.ctx`, `imprint.vectors`).
- At call time: look up the binding, package the call args, hand to `EngramClient`, await the response.

The Axon **never**:

- builds a Signal envelope,
- knows what a trace_id is,
- subscribes to anything,
- knows whether the Synapse is NATS, Kafka, or in-memory.

The Neuron **never** sees `EngramClient` either. It only sees the helpers the Axon injects.

This keeps three layers cleanly separated:

```
Neuron         -  pure fn; sees recall.ctx(...) / imprint.ctx(...)
   ▼ helper call
Axon           -  validates binding, packages args
   ▼ EngramClient call (in-process)
Dendrite       -  builds envelope, owns Synapse, correlates by parent_id
   ▼ Synapse
Engram Dendrite (other process)  -  services the request
```

The same `EngramClient` interface is what the Cortex's own `dendrite.recall(...)` / `dendrite.imprint(...)` helpers call. The Cortex case just sets `trace_id` and `parent_id` from its own dispatch context instead of an active TASK.

### 5.9 Caller ergonomics

```python
# fire-and-forget imprint
await cortex.imprint(
    engram_kind="semantic",
    op="append",
    entry={"content": "...", "tags": ["k8s"]},
)

# competitive recall  -  returns merged hits across all responding Engrams
hits = await cortex.recall(
    engram_kind="semantic",
    query={"text": "...", "top_k": 5},
    deadline_ms=400,
)
```

Under the hood `recall` is `dispatch_task`-shaped: emit `RECALL`, collect `RECALLED` until deadline, merge.

---

## 6. Backend matrix

| Backend            | `engram_kind`(s)        | Notes |
|---|---|---|
| `SqliteEngram`     | `relational`, `keyvalue` | Single-file, default for dev. Built on the existing storage/sqlite plumbing. |
| `PostgresEngram`   | `relational`, `keyvalue` | Shares the pool with `PostgresRegistryStore` if desired. |
| `PgVectorEngram`   | `semantic`              | pgvector extension. Embedding fn is caller-supplied. |
| `QdrantEngram`     | `semantic`              | External vector DB. |
| `S3Engram`         | `blob`                  | `recall` is by key/prefix, no search. |
| `InMemoryEngram`   | any                     | Tests, ephemeral runs. |

A single backend can register **multiple** Engrams of different kinds (e.g. Postgres serving both `relational` and `keyvalue`)  -  each is a separate `attach_engram` call with its own `engram_id`.

---

## 7. Relational schema (sqlite / postgres)

A minimal, kind-agnostic schema for the default relational Engram. Extra columns can be added per-deployment without breaking the protocol because the wire payload is always JSON.

```sql
CREATE TABLE engram_entries (
    id              TEXT PRIMARY KEY,             -- eng_<ULID>
    engram_kind     TEXT NOT NULL,
    merge_key       TEXT,                         -- optional grouping key
    trace_id        TEXT,                         -- originating trace
    neuron_id       TEXT,                         -- who imprinted it
    content         JSONB NOT NULL,               -- the entry body
    tags            TEXT[] NOT NULL DEFAULT '{}',
    version         INTEGER NOT NULL DEFAULT 1,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at      TIMESTAMPTZ
);

CREATE INDEX engram_kind_idx     ON engram_entries (engram_kind);
CREATE INDEX engram_merge_key_idx ON engram_entries (merge_key) WHERE merge_key IS NOT NULL;
CREATE INDEX engram_trace_idx    ON engram_entries (trace_id);
CREATE INDEX engram_tags_gin     ON engram_entries USING gin (tags);
CREATE INDEX engram_content_gin  ON engram_entries USING gin (content jsonb_path_ops);

-- append log for sequence-style entries (op=append)
CREATE TABLE engram_append_log (
    seq         BIGSERIAL PRIMARY KEY,
    merge_key   TEXT NOT NULL,
    entry_id    TEXT NOT NULL REFERENCES engram_entries(id) ON DELETE CASCADE,
    appended_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX engram_append_key_idx ON engram_append_log (merge_key, seq);

-- imprint dedupe (idempotency key from signal id)
CREATE TABLE engram_imprint_seen (
    imprint_id  TEXT PRIMARY KEY,                 -- the IMPRINT event id
    entry_id    TEXT NOT NULL,
    seen_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Sqlite variant: drop `JSONB`/`gin`, use `TEXT` for JSON, store tags as comma-joined `TEXT` with `LIKE` lookups, drop `engram_imprint_seen.seen_at` default to `CURRENT_TIMESTAMP`.

A `PgVectorEngram` adds:

```sql
ALTER TABLE engram_entries ADD COLUMN embedding vector(1024);
CREATE INDEX engram_embedding_idx
    ON engram_entries USING ivfflat (embedding vector_cosine_ops);
```

---

## 8. Event flow

All flows below run inside a single `trace_id = T`. Every envelope carries it; the diagrams show only `parent_id` for clarity.

### 8.1 Neuron mid-task  -  the canonical flow

```
Cortex → TASK[T] (parent: none)                          neuron=web_research
            ▼
       web_research Axon receives TASK
            │
            ├─ recall helper ─► RECALL[R₁] (parent=T, engram_id=ctx-default)
            │                         ▼
            │                   ctx-default Engram Dendrite
            │                         ▼
            │                   RECALLED[r₁] (parent=R₁) ─► Axon resolves Future
            │
            ├─ (optional) sub-TASK[T'] (parent=T) ─► mcp_web_search Neuron ─► AGENT_OUTPUT
            │
            ├─ imprint helper ─► IMPRINT[I₁] (parent=T, engram_id=ctx-default, op=append)
            │                         ▼
            │                   ctx-default Engram Dendrite
            │                   • dedupe by I₁ (imprint_seen)
            │                   • write entry, append log
            │                         ▼
            │                   IMPRINTED[i₁] (parent=I₁)
            │
            ▼
       AGENT_OUTPUT (parent=T)
            ▼
Cortex → FINAL (parent=T)
```

All eight envelopes share `trace_id = T`. The Cortex's budget and deadline cover the whole subtree. A Doppler tailing `T` sees the entire memory access pattern in order.

### 8.2 Imprint routing

```
Neuron's Axon → IMPRINT(engram_id=ctx-default, op=append)
   │
   ├─→ ctx-default (semantic) Engram Dendrite
   │     • dedupe by IMPRINT.id (imprint_seen)
   │     • compute embedding (if entry.embed=true)
   │     • insert into engram_entries + engram_append_log
   │     → IMPRINTED(parent_id=IMPRINT.id, id=eng_…, version=1)
   │
   └─→ relational Engram Dendrite
         (skips  -  engram_id mismatch)
```

### 8.3 Recall routing (addressed, default)

```
Neuron's Axon → RECALL(engram_id=ctx-default, recall_mode=first)
   │
   └─→ ctx-default Engram → RECALLED(hits=[…], took_ms=38) → Axon resolves Future
```

The Cortex helper `recall(...)` resolves on the first `RECALLED` whose `parent_id` matches its emitted RECALL. Other Engrams of the same kind, if any, drop the message because `engram_id` did not match.

### 8.4 Recall fan-out (opt-in)

```
Cortex → RECALL(engram_kind=semantic, recall_mode=merge, deadline_ms=400)
   │
   ├─→ PgVectorEngram     → RECALLED(hits=[…], took_ms=38)
   ├─→ QdrantEngram       → RECALLED(hits=[…], took_ms=51)
   └─→ deadline elapses   → caller merges by score, dedups by entry.id
```

### 8.5 Recall-then-imprint (cache fill)

The same primitives compose. A `PgVectorEngram` that misses can itself emit a `RECALL` with `engram_kind=blob` to hydrate, then `IMPRINT` the rehydrated entry. This is how warm caches and projection materialisation fall out for free.

---

## 9. Dendrite handlers

Mirrors the existing `on_*` decorators:

```python
@dendrite.on_recalled(engram_kind="semantic")
async def cache_hits(sig): ...

@dendrite.on_imprinted()
async def audit(sig): ...
```

`Dendrite.attach_engram(engram)` is the only new public method. Everything else reuses the existing wire / subscription machinery.

---

## 10. Open questions

1. **Recall fan-out vs queue group.** Should every kind-matching Engram answer, or should NATS queue groups pick one? Default to fan-out (multiple sources merge), let callers opt into queue groups via a Synapse-level subject convention.
2. **Backpressure on IMPRINT.** A slow Engram can buffer indefinitely. Same answer as REGISTER/HEARTBEAT: bounded queues, drop with `ERROR { kind: "engram_overloaded" }`.
3. **Cross-Engram consistency.** Out of scope. If a workflow needs two Engrams to agree, do it with `CONSENSUS`.
4. **Encryption at rest.** Engram's problem, not the protocol's. Document a recommended pattern.
5. **Retention.** Add an optional `ttl_s` to `IMPRINT.payload`. Engrams that ignore it just keep the data.
6. **Vector embeddings.** The embed function is caller-supplied to keep model-agnosticism. `embed: true` in the entry is a hint, not a mandate.

---

## 11. Critique of the proposed design

What's strong:

- **Symmetry with TASK/AGENT_OUTPUT.** Reusing the same request/response shape (`RECALL`/`RECALLED`, `IMPRINT`/`IMPRINTED`) means no new mental model. Cortex code that already handles `dispatch_task` patterns reads identically.
- **Plural storage by construction.** Multiple Engrams over one namespace falls out of the existing subscription model. No new router.
- **Lifecycle reuse.** REGISTER/HEARTBEAT/DEREGISTER/DISCOVER need zero changes  -  Engrams are just participants with a different `role`.
- **Backwards-compatible.** `MEMORY_APPEND` stays valid as a thin alias for `IMPRINT { op: "append" }`.

What's risky:

- **Multiple Engrams of the same kind.** The default (`recall_mode: "first"`) assumes one Engram per purpose, which matches the intended deployment (one context Engram, one vector Engram, one blob Engram). Fan-out is opt-in via `"merge"` / `"all"`. If someone runs two `semantic` Engrams without setting `recall_mode`, the first one to answer wins and the other silently does work it discards  -  document this as the trade-off.
- **`op = merge` is fragile.** Deep-merging arbitrary JSON has no obvious right answer (replace vs concat arrays, null handling, etc.). Recommend Engrams declare their merge strategy in capabilities (`merge: "jsonpatch"`, `merge: "deep"`) and reject `merge` ops they don't support.
- **`engram_kind` is doing two jobs.** It's both a routing topic and a schema hint. If we ever want one Engram to serve multiple kinds, that's already covered by attaching it twice, but the spec should be explicit that `engram_kind` is a routing label, not a type.
- **Idempotency window.** `engram_imprint_seen` grows forever. Needs a TTL sweep or partitioned-by-day storage.
- **No transactional multi-imprint.** Workflows that need "imprint A and B atomically" can't get it. Probably fine  -  if you need ACID, hit Postgres directly through a Neuron. Document it explicitly.

What we should defer:

- Indexes over `content` JSON beyond GIN.
- Streaming `RECALLED` (multiple partial results per query).
- Cross-namespace federation.

---

## 12. Summary of additions

New envelope types: `RECALL`, `RECALLED`, `IMPRINT`, `IMPRINTED`.
New ULID prefix: `eng_`.
New SDK class: `Engram` (ABC) + `attach_engram` on `Dendrite`.
New backends: `SqliteEngram`, `PostgresEngram`, `PgVectorEngram`, `InMemoryEngram` (others follow the same ABC).
Reused: `RegistryStore`, REGISTER/HEARTBEAT/DEREGISTER/DISCOVER, the Synapse subscription model, ULID identifiers, payload-is-JSON convention.
Aliased: `MEMORY_APPEND` ≡ `IMPRINT { op: "append" }`.

The protocol version stays `"1"` because every change is additive.
