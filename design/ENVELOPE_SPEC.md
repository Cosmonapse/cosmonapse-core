# Cosmonapse Envelope Specification

**Version:** 1.0.0-draft
**Status:** Draft
**Last updated:** 2026-06-22

---

## 1. Purpose

The envelope is the single shared contract of the Cosmonapse protocol. Every message that crosses a Cosmonapse channel  -  regardless of who produced it, what synapse carries it, or what router or workflow manager is running  -  must be a valid envelope.

This document is the authoritative reference. The SDK, the CLI validator, and any third-party implementation derive their correctness from it. The Python (`cosmonapse.envelope`) and TypeScript (`@cosmonapse/sdk` envelope) bindings are byte-compatible projections of this format.

---

## 2. Design principles

- **Minimal by default.** Only fields needed for routing, tracing, and lifecycle management live at the top level. Everything else belongs in `payload` or `meta`.
- **Strict where it matters.** `id`, `trace_id`, `type`, and `ts` are always required. No exceptions.
- **One addressing field.** All routing identity lives in the single `directed` object (`id` / `type` / `capabilities`). There is no top-level `neuron` field. See §5.5.
- **Neurons are black boxes.** A Neuron receives `(input, context)` and returns `output`. It has no knowledge of the protocol, envelopes, trace IDs, or workflow semantics. The Axon is the only component that translates between the Neuron's raw I/O and the envelope format.
- **The Dendrite owns the wire.** All envelope publishing  -  REGISTER, HEARTBEAT, DEREGISTER, the reply to a TASK  -  is the Dendrite's responsibility. The Axon produces the Signal object; it never touches the Synapse directly.
- **No lifecycle rules.** The spec defines what a valid envelope looks like. It does not define what sequence of envelopes constitutes a valid workflow. Task lifecycle, error handling, retry logic, and termination conditions are entirely the developer's responsibility  -  implemented in their orchestrating Dendrite (or in cooperating Dendrites for the decentralised case).
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
│  (no separate Cortex class  -  `Cortex` is an alias.) │
└────────────────────────┬────────────────────────────┘
                         │  TASK in / AGENT_OUTPUT|CLARIFICATION|PERMISSION|ERROR out
┌────────────────────────▼────────────────────────────┐
│  Axon  (agent-side tool)                            │
│  unwraps TASK -> calls Neuron                       │
│  wraps Neuron output -> AGENT_OUTPUT /              │
│  CLARIFICATION / PERMISSION / ERROR -> Dendrite     │
└────────────────────────┬────────────────────────────┘
                         │  (input, context)  /  output
┌────────────────────────▼────────────────────────────┐
│  Neuron                                             │
│  pure function  -  no protocol knowledge              │
│  fn handle(input: JSON, context: list[Any]) -> JSON │
└─────────────────────────────────────────────────────┘
```

### 3.1 Neuron

Receives `(input, context)`. Returns `output`. Has no knowledge of envelopes, trace IDs, routing, or workflow rules. This is intentional  -  the Neuron is replaceable without touching any infrastructure.

### 3.2 Axon

The agent-side tool. Its complete job:

1. Receive a `TASK` Signal from the Dendrite.
2. Extract `payload.input` and `payload.context_ref`.
3. Resolve `context` via its configured context fetcher.
4. Call the Neuron with `(input, context)`.
5. Produce one of:
   - `AGENT_OUTPUT`  -  the Neuron returned normally.
   - `CLARIFICATION`  -  the Neuron's output contained the `__clarification__` marker.
   - `PERMISSION`  -  the Neuron's output contained the `__permission__` marker.
   - `ERROR`  -  the Neuron raised.
6. Never touch the Synapse  -  it hands the Signal back to the Dendrite to publish.

The Axon's permitted output set (`AXON_TYPES`) is exactly:
`AGENT_OUTPUT`, `CLARIFICATION`, `PERMISSION`, `ERROR`, plus the three participant-lifecycle types `REGISTER`, `DEREGISTER`, `HEARTBEAT`. The lifecycle types describe the Axon's *own participant identity* (id, kind, capabilities); the Axon owns that metadata but the **Dendrite is what actually publishes them onto the Synapse** (§7.6). The Axon never produces workflow or routing envelopes (`TASK`, `FINAL`, `THOUGHT_DELTA`, `TASK_OFFER`, …).

### 3.3 Dendrite

The synapse-side participant. The caller builds the Synapse and passes it in; the Dendrite does NOT own it. Behaviour is opt-in:

- **With attached Axons**  -  emits `REGISTER` on start, `HEARTBEAT` periodically, `DEREGISTER` on stop. Subscribes to TASK on the namespace and routes by the inbound Signal's `directed` field (`directed.id` first, then `directed.type`, then `directed.capabilities`). Publishes the Axon's returned Signal (`AGENT_OUTPUT` / `CLARIFICATION` / `PERMISSION` / `ERROR`).
- **With registered handlers**  -  subscribes to that type on the bus and dispatches matching Signals.
- **With a `registry_store`**  -  mirrors its own attached Axons into the store and auto-subscribes to REGISTER/DEREGISTER/HEARTBEAT so the store tracks the namespace-wide view.
- **Always**  -  exposes orchestration primitives (`dispatch_task`, `emit_final`, `emit_error`, `emit`, the handler decorators) for whoever wants to use them. The Dendrite refuses to emit anything outside `SYNAPSE_TYPES`.

There is no separate Cortex class  -  every Dendrite can orchestrate. `Cortex` is kept as a back-compat alias.

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
  "directed":  { "id": "claude-debug", "type": null, "capabilities": [] },
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
| `v`        | string | Envelope version. `"1"` (or `"1.x"`) for this revision. Decoders MUST reject a different major version and MUST accept any `1.x`, ignoring unknown payload/meta fields. See §9. |
| `id`       | string | Unique event identifier. Prefixed ULID. See §6. |
| `trace_id` | string | Root workflow identifier. Stable across the full delegation tree. See §6. |
| `type`     | string | Message type. Must be one of the values in §7. |
| `ts`       | string | RFC 3339 UTC timestamp with millisecond precision. |

### 5.2 Conditional fields

| Field       | Type   | Required when                              | Description |
|---|---|---|---|
| `directed`  | object | Whenever the Signal must be routed or carries a participant identity (TASK, BID, REGISTER, RECALL, …) | Unified addressing object. See §5.5. Omitted (or `null`) for client-originated, purely-observational, or broadcast envelopes that need no addressing. |
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
| `seq`                | integer | Monotonic sequence number within a `(trace_id, directed.id, type)` triple. |
| `synapse_hops`       | integer | Number of synapse-level hops this envelope has traversed.  |
| `capability_score`   | number  | Orchestrator's capability match score at dispatch time (0–1). |

### 5.5 The `directed` addressing object

All routing identity lives in one object. It has three optional fields, applied in precedence order on the receiving side:

| Field          | Type     | Meaning |
|---|---|---|
| `id`           | string \| null | Direct address. A `neuron_id` for TASK-family routing, or an `engram_id` for RECALL/IMPRINT. Also carries pure producer identity (who emitted a reply). |
| `type`         | string \| null | Type-based routing. A `neuron_kind`, or an `engram_kind` (`"context"`, `"vectors"`, …). |
| `capabilities` | string[] | Capability-based routing. The receiver matches when its capabilities are a superset of this list. |

**Precedence:** `id` wins over `type`, which wins over `capabilities`. All three are optional, so the same object can carry a pure producer identity (`id` only), a typed address, or a capability request. An absent or empty `directed` means "no addressing information."

A reply (`Signal.reply` / `reply()`) propagates the source Signal's `directed` unless overridden, so a responder keeps the addressing context (e.g. echoing back which neuron produced the chain).

---

## 6. Identifier format

All identifiers are **prefixed ULIDs**: a short lowercase type prefix, an underscore, and a 26-character canonical ULID.

```
evt_01JVBCDEF1234567890ABCDEF   ← event id
trc_01JVBCDEF0000000000000000   ← trace id
eng_01JVBCDEF0000000000000002   ← engram entry id (payload-level, see ENGRAM_DESIGN.md)
```

| Prefix | Used for |
|---|---|
| `evt_` | Any envelope `id` |
| `trc_` | Any `trace_id` |
| `eng_` | An Engram entry id (carried in payloads, never as an envelope `id`) |

Implementations MUST generate new ULIDs for each envelope. Reusing an `id` is a protocol violation. Consumers MUST de-duplicate by `id` within a sliding window (recommended minimum: 60 seconds).

---

## 7. Message types

Message types are grouped into categories. All type strings are uppercase.

Each type is annotated with which layer produces it:

| Symbol | Produced by |
|---|---|
| `[D]`  | Dendrite (any Dendrite, including those used as orchestrators) |
| `[A]`  | Axon (produces the Signal object; the Dendrite publishes it) |
| `[WM]` | External Workflow Manager (root task originator outside any Dendrite) |

### 7.1 Lifecycle

#### `TASK`  `[D]` `[WM]`

Assigns a unit of work to a Neuron. Produced by an orchestrating Dendrite (or an external workflow manager for a root TASK).

```json
{
  "type": "TASK",
  "directed": { "id": "answerer" },
  "payload": {
    "input":       { },
    "context_ref": "ctx://debug/session-7",
    "capabilities": ["debugging"],
    "finalize":    false
  }
}
```

| Payload field  | Required | Description |
|---|---|---|
| `input`        | yes      | Arbitrary JSON. The Neuron receives this. |
| `context_ref`  | no       | URI of an existing context: `ctx://<kind>/<id>[@<version>]`. |
| `capabilities` | no       | Capability hints copied through for capability-routed dispatch. |
| `finalize`     | no       | Boolean (default false). Terminal-handler finalize: the worker Dendrite that runs the addressed/routed Axon promotes a successful `AGENT_OUTPUT` by also emitting `FINAL` on the trace (parented to the `AGENT_OUTPUT`, attributed to the producing Neuron). Set automatically by `dispatch(scope="terminal")`. Only `AGENT_OUTPUT` is promoted: `CLARIFICATION`/`PERMISSION` pause the workflow and `ERROR` is already terminal. Dispatchers that orchestrate multi-step work leave this unset and emit `FINAL` themselves. |

The Dendrite uses the `directed` field to route the TASK to the matching attached Axon (`directed.id`, then `directed.type`, then `directed.capabilities`).

---

#### `AGENT_OUTPUT`  `[A]` (Axon produces the Signal; Dendrite publishes)

Contains the raw output from the Neuron with no workflow interpretation. The orchestrating Dendrite receives this and decides what it becomes.

```json
{
  "type": "AGENT_OUTPUT",
  "directed": { "id": "answerer" },
  "payload": { "output": { } }
}
```

| Payload field | Required | Description |
|---|---|---|
| `output`      | yes      | The raw return value of the Neuron. Arbitrary JSON. |

---

#### `FINAL`  `[D]`

Signals successful completion of a task. Produced by an orchestrating Dendrite after it has processed one or more `AGENT_OUTPUT` envelopes according to its workflow rules — or by the worker Dendrite itself when the TASK carried `payload.finalize` (terminal-handler finalize, see `TASK`). Exactly one `FINAL` per task.

```json
{
  "type": "FINAL",
  "payload": { "result": { }, "cost": { } }
}
```

| Payload field | Required | Description |
|---|---|---|
| `result`      | yes      | The terminal result. Arbitrary JSON. |
| `cost`        | no       | Optional rolled-up cost accounting for the trace. |

---

#### `ERROR`  `[D]` `[A]`

Signals that a task has failed. Produced by the Dendrite when the Axon's Neuron raises, or by an orchestrating Dendrite on timeout / no-capable-neuron / budget exceeded. The first terminal event (`FINAL` or `ERROR`) for a trace wins.

```json
{
  "type": "ERROR",
  "payload": {
    "code":        "timeout",
    "message":     "Handler exceeded deadline",
    "recoverable": true
  }
}
```

| Payload field | Required | Description |
|---|---|---|
| `code`        | yes      | Short machine-readable error kind (e.g. `"timeout"`, `"no_capable_neuron"`). |
| `message`     | yes      | Human-readable description. |
| `recoverable` | no       | Boolean (default false). Whether the caller may retry or reroute. |

---

### 7.2 Routing  `[D]`

Orchestrator-produced; Dendrites pass them through if any Axon is meant to react. The Axon itself never inspects them  -  the orchestrating Dendrite coordinates.

#### `TASK_OFFER`

```json
{
  "type": "TASK_OFFER",
  "payload": {
    "input":        { },
    "capabilities": ["debugging", "python"],
    "deadline_ms":  500
  }
}
```

| Payload field  | Required | Description |
|---|---|---|
| `input`        | yes      | The work payload offered to candidate bidders. |
| `capabilities` | no       | Capabilities a bidder should satisfy. |
| `deadline_ms`  | no       | Bid window in milliseconds. |

#### `BID`

`parent_id` must point to the `TASK_OFFER` this bid responds to. `directed.id` identifies the bidding neuron.

```json
{
  "type": "BID",
  "directed": { "id": "claude-debug" },
  "payload": {
    "cost":       0.028,
    "eta_ms":     1800,
    "confidence": 0.93
  }
}
```

| Payload field | Required | Description |
|---|---|---|
| `cost`        | yes      | Estimated cost of doing the work. |
| `eta_ms`      | no       | Estimated time to completion in milliseconds. |
| `confidence`  | no       | Bidder confidence (0–1). |

#### `TASK_AWARDED`

Awards a `TASK_OFFER` to one bidder. The winning Axon's Dendrite treats this exactly like a `TASK`.

```json
{
  "type": "TASK_AWARDED",
  "directed": { "id": "claude-debug" },
  "payload": {
    "input":       { },
    "winning_bid": { "cost": 0.028, "eta_ms": 1800, "confidence": 0.93 },
    "context_ref": "ctx://debug/session-7",
    "finalize":    false
  }
}
```

| Payload field | Required | Description |
|---|---|---|
| `input`       | yes      | The work payload (same shape the winner would have received as a `TASK`). |
| `winning_bid` | no       | The accepted bid, echoed for observability. |
| `context_ref` | no       | Context URI for the awarded work. |
| `finalize`    | no       | Terminal-handler-finalize tag propagated from the offering dispatcher (`dispatch_offer(scope="terminal")`); the winner's Dendrite copies it into the TASK it synthesises so the awarded work finalizes exactly like an addressed `TASK` with `payload.finalize` set. |

#### `TASK_DECLINED`

Producers emit this for losing bidders after picking a winner (informational); workers may emit it proactively to signal they will not bid.

```json
{ "type": "TASK_DECLINED", "payload": { "reason": "capacity" } }
```

| Payload field | Required | Description |
|---|---|---|
| `reason`      | no       | Why the offer was declined. |

### 7.3 Cognition  `[D]`

All cognition events are produced by an orchestrating **Dendrite**. They are optional. A Dendrite that simply maps TASK -> FINAL with no intermediate events is fully compliant.

#### `THOUGHT_DELTA`

```json
{
  "type": "THOUGHT_DELTA",
  "payload": { "delta": "the NPE comes from a null id...", "seq": 3 }
}
```

| Payload field | Required | Description |
|---|---|---|
| `delta`       | yes      | A chunk of streaming reasoning text. |
| `seq`         | no       | Monotonic chunk sequence number within the stream. |

#### `PLAN`

```json
{
  "type": "PLAN",
  "payload": {
    "steps":     [ { "step": "isolate the NPE" }, { "step": "patch UserService:42" } ],
    "rationale": "fix the null deref before touching callers"
  }
}
```

| Payload field | Required | Description |
|---|---|---|
| `steps`       | yes      | Ordered list of step objects. |
| `rationale`   | no       | Why the plan is shaped this way. |

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
    "tool":    "read_file",
    "result":  { },
    "error":   null,
    "call_id": "call_01JV..."
  }
}
```

`TOOL_CALL` requires `tool` and `args`; `call_id` is optional. `TOOL_RESULT` requires `tool` and carries exactly one of `result` / `error`, plus an optional `call_id` correlating it to the call.

### 7.4 Memory  `[D]`

#### `MEMORY_APPEND`

A simple key/value append onto the shared workflow scratch memory. (For the richer Engram store-and-recall protocol, see §7.8 and ENGRAM_DESIGN.md.)

```json
{
  "type": "MEMORY_APPEND",
  "payload": { "key": "finding", "value": { "content": "...", "tags": ["k8s"] } }
}
```

| Payload field | Required | Description |
|---|---|---|
| `key`         | yes      | The memory slot to append under. |
| `value`       | yes      | Arbitrary JSON value. |

#### `ESCALATION`

```json
{
  "type": "ESCALATION",
  "payload": {
    "reason":  "needs GPU inference",
    "target":  "gpu-worker",
    "context": { "model": "llama-70b" }
  }
}
```

| Payload field | Required | Description |
|---|---|---|
| `reason`      | yes      | Why the task is being escalated. |
| `target`     | no       | A higher-authority Neuron to escalate to. |
| `context`     | no       | Arbitrary supporting JSON. |

### 7.5 Coordination  `[D]` / `[A]`

#### `CONSENSUS`  `[D]`

```json
{
  "type": "CONSENSUS",
  "payload": {
    "members": ["claude-debug", "gpt-review"],
    "verdict": "approved",
    "votes":   { "claude-debug": "approve", "gpt-review": "approve" }
  }
}
```

| Payload field | Required | Description |
|---|---|---|
| `members`     | yes      | The participants whose votes were tallied. |
| `verdict`     | yes      | The consensus outcome. |
| `votes`       | no       | Per-member vote detail. |

#### `CONTEXT_SYNC`  `[D]`

```json
{ "type": "CONTEXT_SYNC", "payload": { "snapshot": { }, "version": "4" } }
```

| Payload field | Required | Description |
|---|---|---|
| `snapshot`    | yes      | The context state being shared/synchronised. |
| `version`     | no       | Snapshot version identifier. |

#### `CRITIQUE`  `[D]`

```json
{
  "type": "CRITIQUE",
  "payload": {
    "target_event_id": "evt_01JV...",
    "issues":          [ { "severity": "warn", "note": "Plan skips input validation." } ],
    "verdict":         "revise"
  }
}
```

| Payload field     | Required | Description |
|---|---|---|
| `target_event_id` | yes      | The `id` of the event being critiqued. |
| `issues`          | yes      | List of issue objects (each typically `severity` + `note`). |
| `verdict`         | yes      | Overall verdict, e.g. `pass` / `fail` / `revise`. |

#### `CLARIFICATION`  `[A]` (Axon produces the Signal; Dendrite publishes)

```json
{
  "type": "CLARIFICATION",
  "directed": { "id": "claude-debug" },
  "payload": {
    "question": "What Python version is the target environment?",
    "context":  { "tables_found": ["users"] }
  }
}
```

An orchestrating Dendrite (or any consumer with the right capability) routes the question to wherever the answer comes from  -  a user, another Neuron, an external lookup  -  then either re-dispatches the original `TASK` with the answer folded into `payload.input`, or emits a discrete `CLARIFICATION_ANSWER`.

#### `PERMISSION`  `[A]` (Axon produces the Signal; Dendrite publishes)

A Neuron asks to perform an action *before* doing it. Same return-and-resume shape as `CLARIFICATION`: the Neuron returns a `__permission__` marker (typically only after a `RECALL` against an Engram of standing grants misses), the Axon wraps it as `PERMISSION`, and an answering Dendrite  -  a central orchestrator or any peer  -  replies.

```json
{
  "type": "PERMISSION",
  "directed": { "id": "claude-debug" },
  "payload": {
    "action": "write_file",
    "scope":  { "path": "/etc/hosts" },
    "reason": "patch needs a hosts entry"
  }
}
```

| Payload field | Required | Description |
|---|---|---|
| `action`      | yes      | The action the Neuron wants permission to perform. |
| `scope`       | no       | Narrows the request (e.g. a path, a resource id). |
| `reason`      | no       | Human-readable justification. |
| `context`     | no       | Arbitrary supporting JSON. |

#### `PERMISSION_DECISION` / `CLARIFICATION_ANSWER`  `[D]`

The verdict for a `PERMISSION` / the answer to a (blocking) `CLARIFICATION`. `parent_id` MUST be the request's `id`. There is **no built-in correlation client**: the answering Dendrite either re-dispatches a `TASK` carrying the decision (`respond_to_permission` / `respond_to_clarification`) so the Neuron resumes and can `IMPRINT` / `RECALL` it from an Engram, or emits one of these discrete signals for a peer or observer to consume. Centralised vs decentralised is purely a question of who subscribes to the request.

```json
{
  "type": "PERMISSION_DECISION",
  "parent_id": "evt_01JV...",
  "payload": { "granted": true, "reason": "allowlisted", "ttl_ms": 3600000 }
}
{
  "type": "CLARIFICATION_ANSWER",
  "parent_id": "evt_01JV...",
  "payload": { "answer": "Python 3.11" }
}
```

| Payload field | Required | Description |
|---|---|---|
| `granted`     | yes (decision) | Boolean verdict for `PERMISSION_DECISION`. |
| `answer`      | yes (answer)   | Free-form answer value for `CLARIFICATION_ANSWER`. |
| `reason`      | no       | Justification for the verdict. |
| `ttl_ms`      | no       | How long the grant is valid, so the caller can cache it in an Engram. |

### 7.6 Agent management  `[D]`

Produced by the **Dendrite** on behalf of each attached participant. Both **Neurons** (via their Axon) and **Engrams** register the same way: the participant owns its metadata (id, kind, capabilities, version); the Dendrite emits the envelopes onto the Synapse.

#### `REGISTER`

Identity is carried in `directed`, not in a top-level field:

- `directed.id`     — the participant id (`neuron_id` or `engram_id`).
- `directed.type`   — the participant **kind**: a `neuron_kind` for Neurons (defaults to `"neuron"`), an `engram_kind` for Engrams (`"context"`, `"vectors"`, …).
- `directed.capabilities` — the capability list (mirrored into `payload.capabilities` for registry stores).

Every REGISTER carries one **universal discriminator**, `payload.role`, with value `"neuron"` or `"engram"`. This is the single field a consumer checks to classify a participant — registry stores, Prism, and dopplers all branch on `payload.role` alone rather than inferring kind from id prefixes or message traffic.

```json
{
  "type": "REGISTER",
  "directed": {
    "id":           "claude-debug",
    "type":         "neuron",
    "capabilities": ["debugging", "error_analysis", "python", "javascript"]
  },
  "payload": {
    "role":         "neuron",
    "capabilities": ["debugging", "error_analysis", "python", "javascript"],
    "version":      "0.0.1"
  }
}
```

| Payload field  | Required | Description |
|---|---|---|
| `role`         | yes      | `"neuron"` or `"engram"`. The universal participant discriminator. |
| `capabilities` | yes      | Capability list (mirror of `directed.capabilities`). |
| `version`      | no       | Participant version string. |
| `engram`       | no       | Legacy `true` flag emitted for Engrams as a back-compat alias of `role: "engram"`. New consumers should read `role`. |

An Engram registration is identical except `directed.type` is its `engram_kind`, `payload.role` is `"engram"`, and the legacy `payload.engram = true` alias is also set:

```json
{
  "type": "REGISTER",
  "directed": { "id": "eng_ctx_main", "type": "context", "capabilities": ["recall", "imprint"] },
  "payload":  { "role": "engram", "engram": true, "capabilities": ["recall", "imprint"] }
}
```

The Dendrite re-emits REGISTER alongside each HEARTBEAT so a late-joining orchestrator catches up without a separate sync mechanism. A `DISCOVER` (§7.7) gives the same snapshot on demand.

#### `DEREGISTER`

Identity is carried in `directed.id`, like REGISTER.

```json
{
  "type": "DEREGISTER",
  "directed": { "id": "claude-debug" },
  "payload": { "reason": "graceful_shutdown" }
}
```

| Payload field | Required | Description |
|---|---|---|
| `reason`      | no       | Why the participant is leaving. |

#### `HEARTBEAT`

Identity is carried in `directed.id`. The SDK emits `status` by default; `load` and `in_flight` are optional well-known extensions.

```json
{
  "type": "HEARTBEAT",
  "directed": { "id": "claude-debug" },
  "payload": { "status": "ok" }
}
```

| Payload field | Required | Description |
|---|---|---|
| `status`      | yes      | Liveness status (default `"ok"`). |
| `load`        | no       | Optional load factor (0–1). |
| `in_flight`   | no       | Optional count of tasks currently being handled. |

### 7.7 Discovery  `[D]`

#### `DISCOVER`

A synapse-side control signal that solicits a REGISTER snapshot from
participants on a namespace. Emitted by anyone that wants a current view
of who's online  -  a Doppler attaching to a running namespace, a new
orchestrator Dendrite populating its `registry_store` on startup, or a
reconnecting peer re-verifying a specific worker. The filter fields live
in the **payload** (not in `directed`, since they select which participants
respond rather than addressing the DISCOVER itself).

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
one heartbeat interval. DISCOVER gives the same snapshot on demand  - 
useful on `MemorySynapse` / `DevSynapse` (which have no broker-level
replay) and for the directed re-verify case which broker replay does
not address.

### 7.8 Engram memory  `[D]`

The Engram store-and-recall protocol. These four types are addressed by
`directed` (`directed.id` = engram_id, or `directed.type` = engram_kind);
at least one must be set on RECALL / IMPRINT. See ENGRAM_DESIGN.md §4 for
the full semantics.

#### `RECALL` / `RECALLED`

```json
{
  "type": "RECALL",
  "directed": { "type": "context" },
  "payload": {
    "query":       { "topic": "auth" },
    "recall_mode": "first",
    "filters":     { "tag": "prod" },
    "min_confidence": 0.5
  }
}
{
  "type": "RECALLED",
  "parent_id": "evt_01JV...",
  "payload": {
    "engram_id": "eng_ctx_main",
    "hits":      [ { "id": "eng_01JV...", "value": { }, "score": 0.82 } ],
    "truncated": false,
    "took_ms":   12
  }
}
```

`RECALL` requires `query` and `recall_mode` (`"first"` | `"merge"` | `"all"`); `filters`, `context_ref`, `deadline_ms`, and `min_confidence` are optional. `RECALLED.parent_id` MUST be the RECALL's id; `engram_id` identifies which Engram responded.

#### `IMPRINT` / `IMPRINTED`

```json
{
  "type": "IMPRINT",
  "directed": { "id": "eng_ctx_main" },
  "payload": {
    "op":        "upsert",
    "entry":     { "key": "grant:write_file", "value": true },
    "merge_key": "key"
  }
}
{
  "type": "IMPRINTED",
  "parent_id": "evt_01JV...",
  "payload": { "engram_id": "eng_ctx_main", "op": "upsert", "id": "eng_01JV...", "version": 2 }
}
```

`IMPRINT.op` is one of `add | append | merge | upsert | delete`; `merge_key` is required when `op` is `merge` or `upsert`. `IMPRINTED.parent_id` MUST be the IMPRINT's id; `id` is the resulting entry id when applicable, and `error` is set when the write failed but the Engram chose to respond rather than emit a separate `ERROR`.

### 7.9 Workflow control  `[D]`

Cooperative cancellation of a whole trace.

#### `STOP` / `STOPPED`

```json
{
  "type": "STOP",
  "payload": { "rollback": false, "reason": "user cancelled" }
}
{
  "type": "STOPPED",
  "parent_id": "evt_01JV...",
  "payload": { "rolled_back": false, "cancelled": 2, "compensated": 0, "node": "worker-1" }
}
```

`STOP` is broadcast on the trace; every Dendrite filters by `trace_id`, cancels in-flight neuron work and engram I/O, and  -  when `rollback` is set  -  replays each hosted Engram's per-trace saga journal in reverse. `rollback` only reverses *Engram* state; external side effects a Neuron caused through an Axon are not reversible unless that Neuron registers its own compensator. Each participant acks with `STOPPED` parented to the STOP's id: `cancelled` counts in-flight tasks cancelled, `compensated` counts journal inverse-ops replayed, `node` is an optional label.

---

## 8. Validation

An envelope is **valid** if and only if:

1. It is well-formed JSON.
2. `v` is present and its major version is `1` (`"1"` or `"1.x"`).
3. `id` is present and starts with `evt_` (canonical form `^evt_[0-9A-Z]{26}$`).
4. `trace_id` is present and starts with `trc_` (canonical form `^trc_[0-9A-Z]{26}$`).
5. `parent_id`, if present, starts with `evt_`.
6. `type` is present and is one of the values in §7.
7. `ts` is present and is a valid RFC 3339 UTC timestamp (ends with `Z`).
8. `directed`, if present, is a JSON object whose `id`/`type` are strings or null and whose `capabilities` is an array of strings.
9. `payload`, if present, is a JSON object.
10. `meta`, if present, is a JSON object.
11. Required payload fields for the given `type` are present and correctly typed.

`cosmo validate` checks rules 1–11. Envelope validity is purely structural; the protocol enforces no sequencing or lifecycle rules.

---

## 9. Versioning

- **`v` is a string**, not a number. Current value: `"1"`; minor revisions may use `"1.x"`. Decoders compare the major component only.
- **Major version increment** signals a breaking change. Implementations must reject envelopes with an unrecognised `v`.
- **Additive changes** (new optional fields, new message types, new well-known `meta` keys) do not increment `v`. Consumers must ignore unknown fields.

---

## 10. Subjects and channel addressing

Envelopes are published on subjects on the channel. The SDK derives subjects from envelope fields. Application code must never construct subjects directly.

Conventional subject patterns:

```
<namespace>.<TYPE>              ← e.g. cosmonapse.prod.TASK
<namespace>.<TYPE>.routed       ← capability-routed dispatch (queue-grouped)
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
  "directed": { "id": "codex-gen" },
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
  "directed": { "id": "claude-debug" },
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
  "directed":  { "id": "claude-debug" },
  "ts":        "2026-05-16T14:22:02.110Z",
  "payload":   { "delta": "The null pointer comes from an unchecked repo.find().", "seq": 3 },
  "meta":      { "model": "claude-sonnet-4-6", "tokens": { "out": 18 }, "cost_micro_usd": 44 }
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

A Doppler process **must never** publish to the channel. Synapse adapters connect non-queue subscribers as non-competing consumer groups so their consumption position never delays delivery to Dendrites.

### 12.2 cosmo doppler

`cosmo doppler` is the built-in Doppler. It subscribes to a Synapse and streams envelopes to stdout as newline-delimited JSON.

```
cosmo doppler                          # everything
cosmo doppler --type AGENT_OUTPUT      # filter by envelope type
cosmo doppler --neuron claude-debug    # filter by directed.id
cosmo doppler --trace trc_01H...       # follow one trace
```

---

*This document is the source of truth for the Cosmonapse protocol. Raise questions and proposed changes as issues before implementing.*
