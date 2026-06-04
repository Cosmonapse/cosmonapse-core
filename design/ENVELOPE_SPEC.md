# Cosmonapse Envelope Specification

**Version:** 1.0.0-draft
**Status:** Draft
**Last updated:** 2026-05-17

---

## 1. Purpose

The envelope is the single shared contract of the Cosmonapse protocol. Every message that crosses a Cosmonapse channel — regardless of who produced it, what synapse carries it, or what router or workflow manager is running — must be a valid envelope.

This document is the authoritative reference. The SDK, the CLI validator, and any third-party implementation derive their correctness from it.

---

## 2. Design principles

- **Minimal by default.** Only fields needed for routing, tracing, and lifecycle management live at the top level. Everything else belongs in `payload` or `meta`.
- **Strict where it matters.** `id`, `trace_id`, `type`, and `ts` are always required. No exceptions.
- **Neurons are black boxes.** A Neuron receives `(input, context)` and returns `output`. It has no knowledge of the protocol, envelopes, trace IDs, or workflow semantics. The Axon is the only component that translates between the Neuron's raw I/O and the envelope format.
- **The Dendrite owns the wire.** All envelope publishing — REGISTER, HEARTBEAT, DEREGISTER, the reply to a TASK — is the Dendrite's responsibility. The Axon never touches the Synapse directly.
- **No lifecycle rules.** The spec defines what a valid envelope looks like. It does not define what sequence of envelopes constitutes a valid workflow. Task lifecycle, error handling, retry logic, and termination conditions are entirely the developer's responsibility — implemented in their Cortex (or in cooperating Dendrites for the decentralised case).
- **Extensible where it does not.** `payload` and `meta` are open objects. Implementations may add fields freely; consumers must ignore unknown fields.
- **Language-agnostic.** The spec is defined in terms of JSON. SDK bindings in Python, TypeScript, or any other language are transformations of this format.

---

## 3. Layers and responsibilities

There are four distinct layers. Each has a single, non-overlapping responsibility.

```
┌─────────────────────────────────────────────────────┐
│  Dendrite                                           │
│  USES the Synapse (does not own it). Emits          │
│  REGISTER / HEARTBEAT / DEREGISTER per attached     │
│  Axon; routes inbound TASKs; publishes Axon         │
│  replies. Optionally orchestrates: dispatches       │
│  TASK / TASK_OFFER / BID / FINAL / CRITIQUE / etc.  │
│  (no separate Cortex class — `Cortex` is an alias.) │
└────────────────────────┬────────────────────────────┘
                         │  TASK in / AGENT_OUTPUT|CLARIFICATION|ERROR out
┌────────────────────────▼────────────────────────────┐
│  Axon  (agent-side tool)                            │
│  unwraps TASK -> calls Neuron                       │
│  wraps Neuron output -> AGENT_OUTPUT /              │
│  CLARIFICATION / ERROR -> Dendrite                  │
└────────────────────────┬────────────────────────────┘
                         │  (input, context)  /  output
┌────────────────────────▼────────────────────────────┐
│  Neuron                                             │
│  pure function — no protocol knowledge              │
│  fn handle(input: JSON, context: list[Any]) -> JSON │
└─────────────────────────────────────────────────────┘
```

### 3.1 Neuron

Receives `(input, context)`. Returns `output`. Has no knowledge of envelopes, trace IDs, routing, or workflow rules. This is intentional — the Neuron is replaceable without touching any infrastructure.

### 3.2 Axon

The agent-side tool. Its complete job:

1. Receive a `TASK` Signal from the Dendrite.
2. Extract `payload.input` and `payload.context_ref`.
3. Resolve `context` via its configured context fetcher.
4. Call the Neuron with `(input, context)`.
5. Return one of:
   - `AGENT_OUTPUT` — the Neuron returned normally.
   - `CLARIFICATION` — the Neuron's output contained the clarification marker.
   - `ERROR` — the Neuron raised.
6. Never touch the Synapse.

The Axon never produces `REGISTER`, `HEARTBEAT`, `DEREGISTER`, `FINAL`, `THOUGHT_DELTA`, or any routing envelope.

### 3.3 Dendrite

The synapse-side participant. The caller builds the Synapse and passes it in; the Dendrite does NOT own it. Behaviour is opt-in:

- **With attached Axons** — emits `REGISTER` on start, `HEARTBEAT` periodically, `DEREGISTER` on stop. Subscribes to TASK on the namespace and routes by `signal.neuron`. Publishes the Axon's returned Signal (`AGENT_OUTPUT` / `CLARIFICATION` / `ERROR`).
- **With registered handlers** — subscribes to that AXON_TYPE on the bus and dispatches matching Signals.
- **With a `registry_store`** — mirrors its own attached Axons into the store and auto-subscribes to REGISTER/DEREGISTER/HEARTBEAT so the store tracks the namespace-wide view.
- **Always** — exposes orchestration primitives (`dispatch_task`, `emit_final`, `emit_error`, `emit`, the handler decorators) for whoever wants to use them. The Dendrite refuses to emit anything outside SYNAPSE_TYPES.

There is no separate Cortex class — every Dendrite can orchestrate. `Cortex` is kept as a back-compat alias.

The decentralised pattern is supported by construction: many Dendrites coexist on the same namespace with no central orchestrator, using lifecycle hooks (`on_connect`, `on_refresh`, `on_schedule`) to discover and reconcile peer state.

---

## 4. Wire format

All envelopes are serialised as **UTF-8 encoded JSON objects**. Compact (no unnecessary whitespace) is preferred on the wire. Pretty-print is acceptable for debugging.

```json
{
  "v":         "1",
  "id":        "evt_01JVBCDEF1234567890ABCDEF",
  "trace_id":  "trc_01JVBCDEF0000000000000000",
  "parent_id": "evt_01JVBCDEF0000000000000001",
  "type":      "THOUGHT_DELTA",
  "neuron":    "claude-debug",
  "ts":        "2026-05-16T14:22:01.391Z",
  "payload":   { "delta": "reading the traceback...", "seq": 1 },
  "meta":      { "model": "claude-sonnet-4-6", "tokens": { "out": 12 } }
}
```

---

## 5. Field reference

### 5.1 Required fields

| Field      | Type   | Description |
|---|---|---|
| `v`        | string | Envelope version. Always `"1"` for this revision. See §9. |
| `id`       | string | Unique event identifier. Prefixed ULID. See §6. |
| `trace_id` | string | Root workflow identifier. Stable across the full delegation tree. See §6. |
| `type`     | string | Message type. Must be one of the values in §7. |
| `ts`       | string | RFC 3339 UTC timestamp with millisecond precision. |

### 5.2 Conditional fields

| Field       | Type   | Required when                              | Description |
|---|---|---|---|
| `neuron`    | string | Produced by an Axon (via the Dendrite) or by a Cortex acting as a Neuron | The emitting Neuron's id. Omitted for client-originated envelopes (e.g. a `TASK` dispatched directly from a workflow trigger). |
| `parent_id` | string | Any event that has a causal parent          | The `id` of the event that caused this one. Absent only on the root `TASK` or `TASK_OFFER` that starts a workflow. |

### 5.3 Optional fields

| Field     | Type   | Description |
|---|---|---|
| `payload` | object | Type-specific data. Shape defined per message type in §7. Defaults to `{}` if omitted. Consumers must not reject envelopes with unknown payload keys. |
| `meta`    | object | Non-semantic metadata. See §5.4. Consumers must not reject envelopes with unknown meta keys. |

### 5.4 Well-known `meta` keys

| Key                  | Type    | Description                                                |
|---|---|---|
| `model`              | string  | Model identifier used to produce this event.               |
| `tokens.in`          | integer | Input tokens consumed.                                     |
| `tokens.out`         | integer | Output tokens produced.                                    |
| `cost_micro_usd`     | integer | Cost in millionths of a USD. Rolled up through delegation. |
| `seq`                | integer | Monotonic sequence number within a `(trace_id, neuron, type)` triple. |
| `synapse_hops`     | integer | Number of synapse-level hops this envelope has traversed.|
| `capability_score`   | number  | Cortex's capability match score at dispatch time (0–1).    |

---

## 6. Identifier format

All identifiers are **prefixed ULIDs**: a short lowercase type prefix, an underscore, and a 26-character canonical ULID.

```
evt_01JVBCDEF1234567890ABCDEF   ← event id
trc_01JVBCDEF0000000000000000   ← trace id
```

| Prefix | Used for |
|---|---|
| `evt_` | Any envelope `id` |
| `trc_` | Any `trace_id` |

Implementations MUST generate new ULIDs for each envelope. Reusing an `id` is a protocol violation. Consumers MUST de-duplicate by `id` within a sliding window (recommended minimum: 60 seconds).

---

## 7. Message types

Message types are grouped into five categories. All type strings are uppercase.

Each type is annotated with which layer produces it:

| Symbol | Produced by |
|---|---|
| `[D]`  | Dendrite (any Dendrite, including those used as orchestrators) |
| `[D]`  | Dendrite   |
| `[A]`  | Axon       |
| `[WM]` | External Workflow Manager (root task originator outside the Cortex) |

### 7.1 Lifecycle

#### `TASK`  `[D]` `[WM]`

Assigns a unit of work to a Neuron. Produced by the Cortex (or an external workflow manager for a root TASK).

```json
{
  "type": "TASK",
  "neuron": "answerer",
  "payload": {
    "input":       { },
    "context_ref": "ctx://debug/session-7",
    "deadline":    "2026-05-16T14:25:00.000Z",
    "budget_usd":  0.05
  }
}
```

| Payload field | Required | Description |
|---|---|---|
| `input`       | yes      | Arbitrary JSON. The Neuron receives this. |
| `context_ref` | no       | URI of an existing context: `ctx://<kind>/<id>[@<version>]`. |
| `deadline`    | no       | RFC 3339 UTC. |
| `budget_usd`  | no       | Maximum total cost the caller will accept. |

The Dendrite uses the top-level `neuron` field to route the TASK to the matching attached Axon.

---

#### `AGENT_OUTPUT`  `[D]` (Dendrite publishes; Axon produces the Signal)

Contains the raw output from the Neuron with no workflow interpretation. The Cortex receives this and decides what it becomes.

```json
{
  "type": "AGENT_OUTPUT",
  "neuron": "answerer",
  "payload": { "output": { } }
}
```

| Payload field | Required | Description |
|---|---|---|
| `output`      | yes      | The raw return value of the Neuron. Arbitrary JSON. |

---

#### `FINAL`  `[D]`

Signals successful completion of a task. Produced by the Cortex after it has processed one or more `AGENT_OUTPUT` envelopes according to its workflow rules. Exactly one `FINAL` per task.

```json
{
  "type": "FINAL",
  "payload": { "result": { } }
}
```

---

#### `ERROR`  `[D]` `[D]`

Signals that a task has failed. Produced by the Dendrite when the Axon's Neuron raises, or by the Cortex on timeout / no-capable-neuron / budget exceeded. The first terminal event (`FINAL` or `ERROR`) for a trace wins.

```json
{
  "type": "ERROR",
  "payload": {
    "kind":      "timeout",
    "message":   "Handler exceeded deadline",
    "retriable": true
  }
}
```

---

### 7.2 Routing  `[D]`

Cortex-produced; Dendrites pass them through if any Axon is meant to react. The Axon itself never inspects them — the Cortex coordinates.

#### `TASK_OFFER`

```json
{
  "type": "TASK_OFFER",
  "payload": {
    "input":         { },
    "required_caps": ["debugging"],
    "suggested_caps": ["python"],
    "context_ref":   "ctx://debug/session-7",
    "deadline":      "2026-05-16T14:25:00.000Z",
    "budget_usd":    0.05,
    "bid_window_ms": 500
  }
}
```

#### `BID`

`parent_id` must point to the `TASK_OFFER` this bid responds to.

```json
{
  "type": "BID",
  "neuron": "claude-debug",
  "payload": {
    "offer_id":          "evt_01JVBCDEF0000000000000001",
    "confidence":        0.93,
    "cost_estimate_usd": 0.028,
    "eta_ms":            1800,
    "reasoning":         "Strong match"
  }
}
```

#### `TASK_AWARDED` / `TASK_DECLINED`

```json
{ "type": "TASK_AWARDED", "payload": { "offer_id": "evt_01JV..." } }
{ "type": "TASK_DECLINED", "payload": { "offer_id": "evt_01JV..." } }
```

### 7.3 Cognition  `[D]`

All cognition events are produced by the **Cortex**. They are optional. A Cortex that simply maps TASK -> FINAL with no intermediate events is fully compliant.

#### `THOUGHT_DELTA`

```json
{
  "type": "THOUGHT_DELTA",
  "payload": { "delta": "the NPE comes from a null id...", "final": false }
}
```

#### `PLAN`

```json
{
  "type": "PLAN",
  "payload": {
    "steps":    ["isolate the NPE", "patch UserService:42"],
    "revision": 1
  }
}
```

#### `TOOL_CALL` / `TOOL_RESULT`

```json
{
  "type": "TOOL_CALL",
  "payload": {
    "tool":    "read_file",
    "args":    { "path": "src/UserService.java" },
    "call_id": "call_01JV..."
  }
}
{
  "type": "TOOL_RESULT",
  "payload": {
    "call_id": "call_01JV...",
    "ok":      true,
    "value":   { },
    "error":   null
  }
}
```

### 7.4 Memory  `[D]`

#### `MEMORY_APPEND`

```json
{
  "type": "MEMORY_APPEND",
  "payload": {
    "entry": { "type": "finding", "content": "...", "tags": ["k8s"] },
    "embed": true
  }
}
```

#### `ESCALATION`

```json
{
  "type": "ESCALATION",
  "payload": { "reason": "needs GPU inference", "hints": ["gpu_inference"] }
}
```

### 7.5 Coordination  `[D]` / `[A]`

#### `CONSENSUS`  `[D]`

```json
{
  "type": "CONSENSUS",
  "payload": {
    "proposal_id": "prop_01JV...",
    "outcome":     "approved",
    "votes":       [ { "agent": "claude-debug", "vote": "approve", "weight": 1.0 } ],
    "threshold":   0.66
  }
}
```

#### `CONTEXT_SYNC`  `[D]`

```json
{ "type": "CONTEXT_SYNC", "payload": { "ref": "ctx://debug/session-7@4", "mode": "replace" } }
```

#### `CRITIQUE`  `[D]`

```json
{
  "type": "CRITIQUE",
  "payload": {
    "target_event_id": "evt_01JV...",
    "severity":        "warn",
    "note":            "Plan skips input validation."
  }
}
```

#### `CLARIFICATION`  `[D]` (Dendrite publishes; Axon produces the Signal)

```json
{
  "type": "CLARIFICATION",
  "neuron": "claude-debug",
  "payload": {
    "question": "What Python version is the target environment?",
    "context":  { "tables_found": ["users"] }
  }
}
```

The Cortex (or any consumer with the right capability) routes the questions to wherever the answer comes from — a user, another Neuron, an external lookup — then re-dispatches the original `TASK` with the answers folded into `payload.input` or an updated `context_ref`.

### 7.6 Agent management  `[D]`

Produced by the **Dendrite** on behalf of each attached Axon. The Axon owns the metadata (neuron_id, capabilities, version); the Dendrite emits the envelopes onto the Synapse.

#### `REGISTER`

```json
{
  "type": "REGISTER",
  "neuron": "claude-debug",
  "payload": {
    "capabilities":   ["debugging", "error_analysis", "python", "javascript"],
    "cost_hint":      "medium",
    "max_concurrent": 4,
    "max_latency_ms": 5000,
    "version":        "0.0.1"
  }
}
```

The Dendrite re-emits REGISTER alongside each HEARTBEAT so a late-joining Cortex catches up without a separate sync mechanism. A `DISCOVER` (§7.7) gives the same snapshot on demand.

#### `DEREGISTER`

```json
{
  "type": "DEREGISTER",
  "neuron": "claude-debug",
  "payload": { "reason": "graceful_shutdown" }
}
```

#### `HEARTBEAT`

```json
{
  "type": "HEARTBEAT",
  "neuron": "claude-debug",
  "payload": { "status": "healthy", "load": 0.4, "in_flight": 2 }
}
```

### 7.7 Discovery  `[D]`

#### `DISCOVER`

A synapse-side control signal that solicits a REGISTER snapshot from
participants on a namespace. Emitted by anyone that wants a current view
of who's online — a Doppler attaching to a running namespace, a new
orchestrator Dendrite populating its `registry_store` on startup, or a
reconnecting peer re-verifying a specific worker. Both the broadcast
and directed forms share the same envelope; the optional `payload`
fields select which.

```json
{
  "type": "DISCOVER",
  "payload": {
    "neuron":       "claude-debug",
    "capabilities": ["python", "debugging"]
  }
}
```

| Payload field   | Required | Description |
|---|---|---|
| `neuron`        | no       | Directed mode: only the Axon with this `neuron_id` responds. Omit for broadcast (every Axon responds). |
| `capabilities`  | no       | Restrict the response to Axons whose capabilities are a superset of this list. Combinable with `neuron`. |

**Response.** A DISCOVER has no dedicated response type. Every Dendrite
with attached Axons that satisfy the filter re-emits a standard
`REGISTER` (§7.6) for each matching Axon. The registry machinery that
already consumes REGISTER handles these identically.

**Thundering-herd mitigation.** Each responding Dendrite waits a small
random jitter (0–100 ms) before emitting REGISTER, so on a namespace
with many participants the responses spread rather than spike.

**Relation to other discovery mechanisms.** DISCOVER complements the
re-register-on-heartbeat behaviour, which catches late joiners within
one heartbeat interval. DISCOVER gives the same snapshot on demand —
useful on `MemorySynapse` / `DevSynapse` (which have no broker-level
replay) and for the directed re-verify case which broker replay does
not address.

---

## 8. Validation

An envelope is **valid** if and only if:

1. It is well-formed JSON.
2. `v` is present and equals `"1"`.
3. `id` is present and matches `^evt_[0-9A-Z]{26}$`.
4. `trace_id` is present and matches `^trc_[0-9A-Z]{26}$`.
5. `parent_id`, if present, matches `^evt_[0-9A-Z]{26}$`.
6. `type` is present and is one of the values in §7.
7. `ts` is present and is a valid RFC 3339 UTC timestamp (ends with `Z`).
8. `payload`, if present, is a JSON object.
9. `meta`, if present, is a JSON object.
10. Required payload fields for the given `type` are present and correctly typed.

`cosmo validate` checks rules 1–10. Envelope validity is purely structural; the protocol enforces no sequencing or lifecycle rules.

---

## 9. Versioning

- **`v` is a string**, not a number. Current value: `"1"`.
- **Major version increment** signals a breaking change. Implementations must reject envelopes with an unrecognised `v`.
- **Additive changes** (new optional fields, new message types, new well-known `meta` keys) do not increment `v`. Consumers must ignore unknown fields.

---

## 10. Subjects and channel addressing

Envelopes are published on subjects on the channel. The SDK derives subjects from envelope fields. Application code must never construct subjects directly.

Conventional subject patterns:

```
<namespace>.<TYPE>              ← e.g. cosmonapse.prod.TASK
```

Routers and dashboards built with the SDK use the channel client's subject resolver rather than constructing strings manually.

---

## 11. Examples

### Minimal valid envelope

```json
{
  "v":        "1",
  "id":       "evt_01JVXAMPLE0000000000000001",
  "trace_id": "trc_01JVXAMPLE0000000000000000",
  "type":     "HEARTBEAT",
  "neuron":   "codex-gen",
  "ts":       "2026-05-16T14:22:01.391Z"
}
```

### Root task dispatch

```json
{
  "v":        "1",
  "id":       "evt_01JVXAMPLE0000000000000002",
  "trace_id": "trc_01JVXAMPLE0000000000000002",
  "type":     "TASK",
  "neuron":   "claude-debug",
  "ts":       "2026-05-16T14:22:01.391Z",
  "payload":  {
    "input":  { "code": "...", "error": "NullPointerException at UserService:42" }
  }
}
```

### Streaming thought mid-task

```json
{
  "v":         "1",
  "id":        "evt_01JVXAMPLE0000000000000005",
  "trace_id":  "trc_01JVXAMPLE0000000000000002",
  "parent_id": "evt_01JVXAMPLE0000000000000002",
  "type":      "THOUGHT_DELTA",
  "neuron":    "claude-debug",
  "ts":        "2026-05-16T14:22:02.110Z",
  "payload":   { "delta": "The null pointer comes from an unchecked repo.find().", "final": false },
  "meta":      { "model": "claude-sonnet-4-6", "tokens": { "out": 18 }, "seq": 3, "cost_micro_usd": 44 }
}
```

---

## 12. Doppler

A Doppler is any process that subscribes to the channel as a **passive, read-only consumer**. The SDK provides the `subscribe` primitive on the Synapse. A Doppler is just a process that uses it without a `queue_group`:

```python
async for envelope in synapse.subscribe("cosmonapse.>", handler):
    ...
```

### 12.1 Passive constraint

A Doppler process **must never** publish to the channel. Synapse adapters connect non-queue subscribers as non-competing consumer groups so their consumption position never delays delivery to Dendrites or Cortices.

### 12.2 cosmo doppler

`cosmo doppler` is the built-in Doppler. It subscribes to a Synapse and streams envelopes to stdout as newline-delimited JSON.

```
cosmo doppler                          # everything
cosmo doppler --type AGENT_OUTPUT      # filter by envelope type
cosmo doppler --neuron claude-debug    # filter by neuron
cosmo doppler --trace trc_01H...       # follow one trace
```

---

*This document is the source of truth for the Cosmonapse protocol. Raise questions and proposed changes as issues before implementing.*
