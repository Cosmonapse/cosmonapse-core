# Cosmonapse — Level-0 Protocol Assessment

**Scope:** SDK + CLI + protocol, Python reference implementation.
**Method:** Base assessment of the protocol layer only. Per request, all *clients
and helpers* (EngramClient, Axon-injected `recall`/`imprint`, Pathway sugar,
`dispatch_and_wait`, convenience decorators) are **excluded from the base** and
counted as **positives** layered on top. The base must hold: at level 0, every
TASK and every sub-task's communication — directed or undirected — crosses the
**Synapse** and follows **dispatch → wait**.

Verdict legend: ✅ holds at base · ⚠️ holds but with a caveat · ❌ gap.

---

## Summary table

| # | Invariant | Verdict |
|---|---|---|
| 1 | Neurons are black boxes | ✅ |
| 2 | Axons connect neurons to dendrites | ✅ |
| 3 | Dendrites are the main communication agent | ✅ |
| 4 | Engrams are storage that sits on the synapse | ✅ |
| 5 | Everyone can register and save registers | ⚠️ |
| 6 | All task / sub-task comms via synapse + dispatch-and-wait at level 0 | ⚠️ |

The architecture is sound and the intent matches every invariant. Two caveats
on (5) and (6) are the only things standing between the code and a clean
"level-0 enforced" claim. Both are narrow.

---

## 1. Neurons are black boxes — ✅

- `neuron.py`, `_neuron_base.py`, `_neuron_mcp.py` import **no** `Signal`,
  `Synapse`, or `envelope` symbols. A Neuron is `async fn(input, context) -> dict`
  and nothing else. (`neuron.py` imports only base/mcp wrappers and stdlib.)
- The Axon detects optional `recall`/`imprint` kwargs by signature inspection
  (`axon.py:97-113`) — the Neuron never imports the helpers, they are injected.
- Provider wrappers (ollama / hf / mcp) keep the same opaque `input -> output`
  shape, so the fabric cannot tell an LLM, an MCP server, and a plain function
  apart. Matches DECISIONS §3.

**Nothing to fix.**

---

## 2. Axons connect neurons to dendrites — ✅

- `Axon` never touches the Synapse. It owns identity (`neuron_id`,
  `capabilities`, `version`) + the neuron body, and returns a Signal from
  `handle_task` (`axon.py:159-233`). Header comment and code agree.
- Attachment is explicit and 1:1-guarded: `attach_to` refuses a second Dendrite
  (`axon.py:121-126`); `Dendrite.attach_axon` refuses a duplicate `neuron_id`
  (`dendrite.py:230-236`).
- Engram access from inside a Neuron is routed Axon → `dendrite.engram_client`
  → Dendrite (`axon.py:240-311`). The Axon packages args; it never builds an
  envelope or subscribes.

**Nothing to fix.**

---

## 3. Dendrites are the main communication agent — ✅

- **Single choke point.** Every outbound path funnels through
  `Dendrite._publish` → `synapse.publish` (`dendrite.py:1665-1666`). Verified for:
  orchestration `emit()` (1641-1663), worker replies `_on_task` (1736-1745),
  TASK dispatch `_publish_task` (847-861), Engram I/O via EngramClient
  (`engram/client.py:152, 207, 224`), lifecycle REGISTER/HEARTBEAT/DEREGISTER.
- **Type guard is enforced in code, not convention.** `emit()` refuses anything
  outside `SYNAPSE_TYPES` (1657-1662); `AXON_TYPES` vs `SYNAPSE_TYPES` are
  disjoint where it matters (`envelope.py:126-176`). Axons can only ever return
  `AGENT_OUTPUT / CLARIFICATION / PERMISSION / ERROR`.
- **Co-located is still on-bus.** Even when an orchestrator and a worker Axon
  share one process on `MemorySynapse`, the TASK and AGENT_OUTPUT are
  `publish`/`subscribe`'d — there is no in-process shortcut. This is the
  strongest evidence for invariant 6.

**Nothing to fix.**

---

## 4. Engrams are storage on the synapse — ✅

- Engram is a synapse-side participant, **not** a Neuron: it never produces
  `AGENT_OUTPUT`; it listens for `RECALL`/`IMPRINT` and answers
  `RECALLED`/`IMPRINTED` (ENGRAM_DESIGN §1, §4). The four types live in
  `SYNAPSE_TYPES`, **not** `AXON_TYPES` (`envelope.py`), so Axons cannot emit
  them directly — they hand off to the Dendrite, same rule as `MEMORY_APPEND`.
- Inbound routing: `_dispatch_inbound` sends `RECALL`→`_on_recall`,
  `IMPRINT`→`_on_imprint`, and delivers `RECALLED`/`IMPRINTED` to the
  EngramClient correlation table (`dendrite.py:1880-1894`). The backend
  (sqlite/postgres/vector) is a black box called **in-process by its hosting
  Dendrite**, but the request/response always crosses the Synapse.
- Engrams reuse REGISTER/HEARTBEAT/DEREGISTER with `role="engram"`
  (`_emit_engram_register`, start path 1791+), so the one registry surface
  already tracks them — no second registry.

**Nothing to fix.** This is the cleanest invariant in the codebase.

---

## 5. Everyone can register and save registers — ⚠️

What holds:
- `registry_store` is optional and accepted for **any** Dendrite regardless of
  role (constructor 100-121). Backends memory/sqlite/postgres conform to one
  ABC (`storage/`, DECISIONS §10).
- Both workers and orchestrators emit REGISTER on start and mirror into their
  store (`start()` 48-49 area). Engrams register too (§4).

Caveat:
- **Registration is fine for everyone; *dispatch* is gated to `role="orchestrator"`.**
  `_require_orchestrator` blocks `dispatch_task` / `dispatch` / `emit` for
  workers (`dendrite.py:216-224`). That is a deliberate, reasonable guard — but
  it means "everyone" is symmetric for *register/save* yet **asymmetric for
  initiating communication**. If the intended invariant is full peer symmetry
  (any peer can both register *and* originate a TASK), the role gate contradicts
  it. If the intent is only "anyone can register + persist," it holds.

Decision needed: is the role split (`orchestrator`/`worker`) part of the level-0
model, or should any peer be able to dispatch? Today a decentralised peer must
be constructed with `role="orchestrator"` to both serve and dispatch — which
works, but the default `role="orchestrator"` plus a `worker` opt-out is the only
thing making this "asymmetric."

---

## 6. All task / sub-task comms via synapse + dispatch-and-wait — ⚠️

What holds at base:
- The Synapse interface is the narrow five — `connect/close/publish/subscribe/
  request` (`synapse/base.py`). `request` *is* a native dispatch-and-wait
  primitive.
- TASK → AGENT_OUTPUT, RECALL → RECALLED, IMPRINT → IMPRINTED all cross the bus
  and are correlated by `id`/`parent_id` futures. Dispatch-and-wait is real and
  uniform for these. (`dispatch_and_wait` and `Pathway` are *helpers* over this —
  counted as positives, not base.)
- Sub-tasks (nested TASKs) inherit `trace_id`; the whole slice is one
  Doppler-tailable stream (ENGRAM_DESIGN §5.4, §8.1).

Two caveats:

**(a) Workers/Neurons cannot create sub-tasks at level 0.**
The Axon injects only `recall` and `imprint` into a Neuron (`axon.py:179-183`).
There is **no** injected `dispatch`/`spawn` helper, and `_require_orchestrator`
blocks a worker Dendrite from emitting TASK. So "every task **and smaller tasks
it creates**" is only satisfiable when the creator is an orchestrator. A pure
worker Neuron that wants to fan out a sub-task has no level-0 path to do so —
it must return control to an orchestrator, or the worker's host must itself be
`role="orchestrator"`. If sub-task spawning *from inside a Neuron* is meant to
be a level-0 capability, it is missing the symmetric primitive that `recall`/
`imprint` already model (inject a `dispatch` helper that the hosting Dendrite
turns into a TASK on the bus, correlated by `parent_id`).

**(b) CLARIFICATION / PERMISSION are not dispatch-and-wait at base.**
Unlike RECALLED/IMPRINTED (which have the EngramClient correlation table), the
clarification/permission round-trip has **no level-0 correlation primitive**.
The `CognitionClient` was removed (`cognition.py` is a tombstone; DECISIONS
§12a). The replacement is "return-marker + re-dispatch a TASK" or "emit a
discrete CLARIFICATION_ANSWER / PERMISSION_DECISION the developer wires by
hand." That is deliberate, but it means this one interaction is **request +
async-resume**, not **dispatch-and-wait** — the asker does not block on a
correlated reply at the protocol layer. If invariant 6 is meant to be literal
("*all* directed/undirected comms are dispatch-and-wait"), clarification/
permission is the exception and should either (i) gain an EngramClient-style
correlation table, or (ii) be explicitly documented as the one ask-and-resume
flow.

**Stale doc to fix regardless:** `envelope.py` (the `SYNAPSE_TYPES` comment near
`PERMISSION_DECISION`/`CLARIFICATION_ANSWER`) still says these are "correlated
by parent_id via its CognitionClient." The CognitionClient no longer exists —
update the comment to match DECISIONS §12a.

---

## Recommended actions (smallest set)

1. **Decide the symmetry question (invariant 5/6a).** Either document the
   `orchestrator`/`worker` role split as intentional level-0 asymmetry, or make
   dispatch a peer-symmetric capability and inject a `dispatch`/`spawn` helper
   into Neurons (mirrors `recall`/`imprint`: Neuron calls helper → Axon packages
   → Dendrite emits TASK on bus → correlates reply by `parent_id`). This is the
   one change that would make "every task and smaller tasks it creates" literally
   true at level 0.
2. **Resolve clarification/permission (invariant 6b).** Add an EngramClient-style
   correlation table for `CLARIFICATION_ANSWER`/`PERMISSION_DECISION`, **or**
   document them as the sanctioned ask-and-resume exception.
3. **Fix the stale `CognitionClient` comment** in `envelope.py`.
4. *(Correction)* The orchestrator-level `dendrite.recall(...)` /
   `dendrite.imprint(...)` helpers **do exist** (`dendrite.py:2176, 2221`), as
   does the full Engram serving path (`_on_recall`/`_on_imprint` →
   `engram.recall`/`imprint` → `recalled_signal`/`imprinted_signal` via the
   un-gated `_publish`, `dendrite.py:2073-2163`). An earlier draft of this note
   claimed they were missing; that was a stale-mount read artifact and is
   retracted.

Everything else — black-box neurons, axon→dendrite boundary, dendrite as the
sole bus toucher, engrams-on-synapse, type-guarded emit — is correct and
enforced in code.
