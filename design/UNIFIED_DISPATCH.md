# Unified Dispatch — one primitive for every request/response

**Status:** Design, pending implementation
**Supersedes:** the split between `dispatch_task`/Pathway (TASK, trace-keyed) and
EngramClient (RECALL/IMPRINT, parent_id-keyed), and the return-marker/resume
flow for CLARIFICATION/PERMISSION.

---

## 1. The single rule

There is **one** way to send anything that expects a reply:

```python
pathway = await dendrite.dispatch(signal_type, ...)      # returns a Pathway
signal  = await dendrite.dispatch_and_wait(signal_type, ...)  # opens, awaits, closes
```

`dispatch` is the universal verb. `signal_type` selects what is sent —
`TASK`, `CLARIFICATION`, `PERMISSION`, `RECALL`, `IMPRINT`, anything in the
request→reply registry. There is **no** `ask`, `recall`, `imprint`, `clarify`
method at the protocol layer; those names, if they survive at all, are
zero-logic aliases that just fill in `signal_type=` and a payload shape.

**The only distinction between signal types:** emitting `signal_type=TASK`
requires `role="orchestrator"`. Every other type is open to any Dendrite,
because a worker servicing a TASK must be able to dispatch a `CLARIFICATION`
or `PERMISSION` mid-task. This is the entire role model.

---

## 2. Correlation: one trace, match on `parent_id`

A sub-request inherits its parent's `trace_id` (the whole job is one slice for
Doppler/cost/deadline). Correlation of a specific reply to a specific request
rides **`parent_id`**:

```
request.id            = X
response.parent_id    = X        ← this is the match
response.trace_id     = request.trace_id   (unchanged across the slice)
```

A Pathway is therefore **predicate-driven**, not hardcoded to `trace_id`:

| Pathway kind        | `match(sig)`                                          | closes on            |
|---|---|---|
| TASK (orchestrator) | `sig.trace_id == trace`                               | FINAL / ERROR        |
| request (any role)  | `sig.parent_id == request.id and sig.type ∈ replies` | first match / ERROR / deadline |

The TASK pathway is the long-lived "watch the whole trace" case (it still sees
AGENT_OUTPUT, PLAN, TOOL_CALL… as intermediate signals and closes on the
terminal). A request pathway is short-lived: open on send, resolve on the first
correlated reply, close.

Both register in the Dendrite's one pathway table and both are fed by the same
`_dispatch_inbound` path. Nesting falls out for free: a CLARIFICATION pathway
opened inside a TASK is just another entry in the table, scoped by its own
`parent_id`, sharing the trace.

---

## 3. Request → reply registry

A single table the dispatcher consults to know what reply type(s) close a
request pathway:

```python
REQUEST_REPLIES: dict[SignalType, frozenset[SignalType]] = {
    SignalType.TASK:          frozenset({SignalType.FINAL, SignalType.ERROR}),     # terminal-correlated by trace
    SignalType.CLARIFICATION: frozenset({SignalType.CLARIFICATION_ANSWER, SignalType.ERROR}),
    SignalType.PERMISSION:    frozenset({SignalType.PERMISSION_DECISION, SignalType.ERROR}),
    SignalType.RECALL:        frozenset({SignalType.RECALLED, SignalType.ERROR}),  # folded in via EngramClient step
    SignalType.IMPRINT:       frozenset({SignalType.IMPRINTED, SignalType.ERROR}),
}
```

`ERROR` is always an accepted terminal so no pathway can hang on a failed slice.

---

## 4. The flow, end to end

The Neuron stays a pure function: it **returns a marker**, the **Axon wraps** it
into a signal, the **Dendrite dispatch-and-waits** and re-invokes the Axon with
the answer. Three layers, unchanged ownership.

```
orchestrator.dispatch_and_wait(TASK, neuron=..., input=...)
   │   opens P0  (match: trace==T, close: FINAL/ERROR)
   ▼
worker Dendrite receives TASK → axon.handle_task(TASK)
   │      └─ Neuron returns {"__clarification__": True, "question": ...}
   │      └─ Axon WRAPS → returns a CLARIFICATION signal (parent_id=TASK.id)   [axon.py:203-226]
   │
   │   worker Dendrite sees the reply is CLARIFICATION → dispatch_and_wait(it):
   │      opens P1 (match: parent_id==CLAR.id, close: CLARIFICATION_ANSWER/ERROR)
   │      ── CLARIFICATION on bus ──►  peer/orchestrator @on(CLARIFICATION) decides
   │      ◄─ CLARIFICATION_ANSWER (parent_id=CLAR.id) ── P1 matches, resolves, CLOSES
   │
   │   worker Dendrite re-invokes axon.handle_task(TASK + answer)
   │      └─ Neuron runs again; its recall now hits / answer is in input → real output
   │      └─ Axon wraps → AGENT_OUTPUT (parent_id=TASK.id)
   ▼
AGENT_OUTPUT → published                ← intermediate on P0
   ▼
orchestrator @on(AGENT_OUTPUT) → dispatch FINAL
   ▼
FINAL → P0 matches, resolves, CLOSES
```

Same machinery for PERMISSION (marker → Axon wraps PERMISSION → Dendrite
dispatch-and-waits PERMISSION_DECISION → re-invoke). RECALL/IMPRINT differ only
in that they are **injected inline helpers** the Neuron awaits mid-function
(no return, no re-invoke) — see §5.

---

## 5. Neuron-side surface — two distinct patterns

There are two ways a Neuron triggers a sub-interaction, and they stay distinct:

**(a) Output markers — CLARIFICATION / PERMISSION. The Axon owns these.**
The Neuron returns a marker; the Axon wraps it into the signal and hands it to
the Dendrite. The Neuron never dispatches and never sees the bus. This is the
existing, correct behavior (`axon.py:203-226`) and it **stays**:

```python
async def my_neuron(input, context):
    if "region" not in input:
        return {"__clarification__": True, "question": "which region?"}
    ...
    return {"answer": ...}
```

The worker Dendrite turns the Axon's CLARIFICATION/PERMISSION reply into a
`dispatch_and_wait` (§4), then re-invokes the Axon with the answer. The Neuron is
re-run, not resumed — pure, stateless, idempotent up to the marker (it typically
`recall`s first so the second run hits).

**(b) Inline helpers — RECALL / IMPRINT. Injected, awaited mid-function.**
These do not end the turn, so they are injected callables the Neuron awaits and
continues past (`axon.py:179-183`). Folded into the unified `dispatch` in the
EngramClient step (§8), but they remain *inline awaits*, not return-markers.

The rule: **anything that ends the Neuron's turn is an Axon-wrapped marker;
anything consumed mid-turn is an injected await.** The Axon is the wrapper for
the former; the Dendrite is the bus for both.

---

## 6. Public API shape

```python
async def dispatch(
    self,
    signal_type: SignalType = SignalType.TASK,
    *,
    directed: Directed | None = None,     # target: neuron / engram / answerer
    payload: dict | None = None,          # type-specific body (input, question, query, entry…)
    trace_id: str | None = None,          # inherit to continue a slice
    parent_id: str | None = None,         # causal parent (the request this answers under)
    meta: dict | None = None,
    scope: str = "all",
) -> Pathway: ...

async def dispatch_and_wait(
    self, signal_type: SignalType = SignalType.TASK, *,
    timeout_s: float | None = 30.0, **kw,
) -> Signal: ...
```

- `signal_type=TASK` → `_require_orchestrator`; all other types skip the gate.
- The envelope is built by the type's existing builder (`task_signal`,
  `clarification_signal`, `recall_signal`, …) from `payload`.
- The Pathway predicate is chosen from `REQUEST_REPLIES`: TASK → trace match;
  everything else → `parent_id == sig.id`.
- Back-compat: today's `dispatch_task` / `dispatch` / `dispatch_and_wait`
  signatures (`neuron=`, `input=`, `capabilities=`) remain valid as the
  `signal_type=TASK` specialization.

---

## 7. What changes in code

| File | Change |
|---|---|
| `pathway.py` | Pathway gains a `match`/`terminal` predicate (trace **or** parent_id). `_TERMINAL_TYPES` becomes per-pathway from the registry. |
| `dendrite.py` | `dispatch`/`dispatch_and_wait` take `signal_type`; build via registry; open the right-keyed Pathway; role gate **only** when `signal_type is TASK`. Inbound routing indexes pathways by both `trace_id` and `parent_id`. |
| `axon.py` | **Unchanged for CLARIFICATION/PERMISSION** — the Axon keeps wrapping the Neuron's `__clarification__`/`__permission__` markers into signals (`axon.py:203-226`). The markers stay. (`recall`/`imprint` injection unchanged until the EngramClient step.) |
| `dendrite.py` `_on_task` | When `axon.handle_task` returns CLARIFICATION/PERMISSION, don't just publish-and-forget: `dispatch_and_wait` the signal (parent_id pathway), then re-invoke `handle_task` with the answer merged into the TASK input/context; loop until AGENT_OUTPUT/ERROR. |
| `envelope.py` | Add `REQUEST_REPLIES`. Fix the stale `CognitionClient` comment. |
| `cognition.py` | Already a tombstone — delete. |

## 8. Out of scope here (next step)

EngramClient folds into this as a thin wrapper: its `_pending_recalls`/
`_pending_imprints` tables ARE parent_id-keyed request pathways. Once `dispatch`
handles RECALL/IMPRINT, EngramClient becomes `dispatch(RECALL/IMPRINT)` plus the
`recall_mode` merge/all fan-in logic layered on top. Deferred per request.
