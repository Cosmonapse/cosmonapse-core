# Glia: per-component policy cards

**Status:** Phases 0 to 4 implemented 2026-09-17 in `packages/python-sdk`:
`cosmonapse/glia/`, the three caller-side checkpoints in `axon.py`, the
guarded wrappers on the Effector and Engram ABCs, and the trace counter on
the Dendrite. Plus the `AUDIT` signal, which overrides hard constraint 1 -
see that constraint below, where the override and its measured cost are
recorded. Phase 5 (identity and signing) and Phase 6 (Genesis and Prism)
are NOT built, and neither is `trace_strict`.

**Section 17 (2026-09-23) rebuilds the card as a two-way gate** on its
component: every policy has a direction and a scope, there are no
checkpoints, and a violation retries as its policy says. Built. Where an
earlier section talks about checkpoints, section 17 wins.

**Section 14's open decisions, settled:** `fail_open` per card only; the
journal is JSONL with no collector in core; `trace_strict` left out; the six
audit kinds are `GUARD_RETRY`, `EVAL_RETRY`, `TOOL_RETRY`,
`TRANSIENT_RETRY`, `LIMIT_REFUSED` and `DEADLINE_ABANDONED`. One decision
remains open; see section 14.

**Read section 16 before trusting a section above it.** Several sections
were superseded by what the build measured rather than by a change of mind:
constraint 1 (a new `SignalType` is now allowed, and why), section 9.2 (the
record is emitted at the event, not at the next attempt's start), section 10
(`trace` mode did not bound a burst), and section 0 (this was called Myelin).
Each correction sits in its own section; section 16 is the index to them,
and records what this document underdetermined and what was chosen instead.

**Scope:** `packages/python-sdk` only. Python is the sole SDK (DECISIONS #19).
**Audience:** whoever implements this. Read the whole file before writing code.

---

## 0. This was called Myelin

Renamed 2026-09-17. Recorded here because the reason is not the obvious one.

The prompt for the rename was that the layer had stopped being about
security. That is true but it does not indict the old name: myelin is an
insulation word, not a defence word, and it read neutral.

**The real defect was anatomical.** Myelin sheathes axons, and nothing else.
A card mounts on an Axon, an Effector, an Engram and a Dendrite. The mismatch
was invisible while the design was written Axon-first, and became real in
Phase 2, when `guarded_invoke`, `guarded_recall` and `guarded_imprint` put
checkpoints on the callee-side ABCs where no axon exists.

**And the card outgrew "sheath".** It now carries policies, the repair loop's
attempt and deadline budgets, trace-wide action limits, the journal, and the
audit sample rate. That is a component's whole operating envelope: what it
may do, how it recovers, what it may spend, and what it records. A sheath
describes one of those four.

`Glia` fixes both. Glial cells are the non-signalling support cells, and
their job is regulating a neuron's environment, maintaining it, cleaning up
and mounting an immune response - limits and repair and audit and guard, not
defence alone. They attach to every neuron type. And myelin is itself a
glial product, made by oligodendrocytes, so the rename reads as the widening
that actually happened rather than as a change of subject.

Section 2 allows exactly one anatomical name below plain words, and this is
still that one name. The management plane named in section 14 still takes a
tool name rather than an anatomy word.

**Deliberately unchanged**, because they name policies, which a Glia holds,
rather than the card itself: `PolicyArtifact`, `PolicyRule`, `PolicyError`,
`PolicyOutcome`, `PolicyRefusal`, `RepairPolicy`, the `policies` field in the
artifact, `policy_id` and `policy_version` on the wire, and
`refused_by: "policy"` on a refused observation. Also unchanged: `card_id`
and `card_hash`, and "the card" as the informal noun for an instance, which
is what section 2's vocabulary table already defines it as.

**Changed:** the class (`Myelin` to `Glia`), the `myelin=` kwargs and
`.myelin` properties on all four primitives plus the three `attach_*`
methods, the package (`cosmonapse.policy` to `cosmonapse.glia`), the wire
field (`meta.policy` to `meta.glia`), this file's name, and the four
`tests/test_policy_*.py` files.

Line numbers below were verified against the tree on 2026-09-17. They drift;
confirm each with grep rather than trusting the number.

---

## 1. What this is

A governance layer for cosmonapse-core. Every participant (Axon, Effector,
Engram, and the Dendrite itself) carries its own **Glia card**: a portable
object holding policies, repair budgets, trace limits, a journal and an audit
rate, inserted into the component rather than written into it. A card
refuses, rewrites or escalates what its component is about to do or about to
serve, bounds how far it may retry and how much it may spend, and records
what it did.

The design is **decentralisation first**. There is no orchestrator and no
central policy process. A card is never consulted over the wire at request
time. Centralisation, if someone wants it, is a count of cards and a choice of
topology at deploy time, not a mode in the SDK.

**There must be no `centralized=True` flag and no code path that only runs
when one exists.**

---

## 2. Vocabulary

Plain words below the one anatomical name. Do not introduce metaphor names.

| Term | Meaning |
|---|---|
| **Glia** | the card. One per participant. |
| **policies** | what a card carries. Not "reflexes", not "rules engine". |
| **limit** | a numeric cap. Not "threshold". |
| **verdict** | `allow` / `deny` / `redact` / `escalate`. |
| **checkpoint** | one of the five interception points in section 6. |

Checkpoint decorators are `on_input`, `on_output`, `on_tool_call`,
`on_recall`, `on_imprint`, matching the existing `@fx.on_tool_call` and
`@engram.on_recall` idiom.

---

## 3. Hard constraints

Violating any of these is a design regression, not a tradeoff.

1. ~~**No new `SignalType`.**~~ **OVERRIDDEN 2026-09-17, deliberately.** The
   original rule and its reason stand recorded: `Signal.type` is a pydantic
   enum field (`envelope.py:281`), so an SDK that predates a member fails to
   decode it, and a verdict can ride `meta.glia` instead, which
   ENVELOPE_SPEC section 2 makes explicitly ignorable.

   Two things changed the calculus. First the blast radius was measured and
   is smaller than this constraint claimed: every Synapse adapter already
   wraps `Signal.decode` in a try and continues (`nats.py:127`,
   `kafka.py:222`, `dev.py:510`), so an old peer logs one warning per
   unknown signal and drops it. Nothing crashes, no subscription dies.
   Second, `meta.glia` is not addressable: a compliance consumer would
   have to decode every signal in the namespace to filter for it, whereas a
   distinct type gives it `cosmonapse.<ns>.AUDIT` and nothing else.

   So `AUDIT` was added, and `THOUGHT_DELTA` removed (it had no producer in
   the SDK and one consumer). The cost is accepted and stated rather than
   hidden: in a mixed-version namespace an audit record can go missing with
   only a log line to show it, which is worse for a compliance record than
   for a cosmetic one. That is why the verdict rides BOTH: `meta.glia` on
   the terminal reply is authoritative and never dropped, and the `AUDIT`
   signal is the addressable copy. Removing the control plane, or running an
   old peer, costs you the stream and not the verdict.
2. **No envelope version bump.** No change to `Signal`, no change to the
   `Synapse` ABC (`synapse/base.py`).
3. **A policy verdict never produces `ERROR`.** It produces the reply the step
   would have produced anyway, with `error` set and `meta.glia` attached.
   `ERROR` keeps its current meaning: the step broke. See section 7 and the
   reasoning in section 12.
4. **Do not remove the role gate.** `_ROLE_GATED_TYPES` is `{TASK, STOP}`
   (`dendrite.py:284`) and `_require_orchestrator` (`dendrite.py:286`) stays.
   A card may *widen* it: a worker whose card grants dispatch may dispatch, a
   worker with no card is refused exactly as today.
5. **Mode defaults to `off`.** Every existing brain must keep working with no
   card present. `enforce` is set by the pod image, never by the library
   default.
6. **The existing 26 test files under `packages/python-sdk/tests` must pass
   untouched.** If one needs editing, stop and raise it. That edit is the
   disruption and it needs a decision, not a workaround.
7. **A redaction returns a copy.** `MemorySynapse` passes the `Signal` object
   by reference and never serialises (grep `encode()` in
   `synapse/memory.py`: no hits). Mutating in place rewrites what every other
   in-process subscriber holds.
8. **Nothing on the bus carries refused content.** Policy id, policy version,
   checkpoint, verdict, and a hash. The matched text goes to the component's
   local journal only. Otherwise the audit record leaks exactly what the
   policy was protecting.
9. **The policy module is public and introspectable from day one.** Genesis
   must be able to render a card editor without hand-mirroring fields. See
   `design/DECISIONS.md` and the `_genesis_ast.py` precedent this exists to
   avoid repeating.

---

## 4. The card

New public package: `cosmonapse/glia/`.

```
cosmonapse/glia/
  __init__.py     public exports
  base.py         Glia, Verdict, checkpoint decorators, mode
  artifact.py     declarative policy bundle: parse, validate, version
  journal.py      local audit sink (append-only file writer)
  identity.py     PHASE 5 ONLY: keypair, canonical form, sign, verify
```

### 4.1 Contents

A card holds:

- `card_id` and the component identity it was issued to
- `policies`: the declarative bundle (section 4.3) plus any decorator-declared
  handlers
- `version`: the policy artifact version, reported on REGISTER
- `issued_at`, `issuer`
- `not_after`: **present in the format, normally unset, never enforced.**
  There is deliberately no card lifetime (section 11). Keeping the field means
  adding expiry later is a policy choice, not a format change.
- PHASE 5: a keypair, and the set of issuer roots this component accepts

### 4.2 Loading and mounting

```python
CARD = Glia.load("corp.card")          # file, bytes, or env var
```

Mounted singular, not as a list. One card, one answer to "what protects this
component":

```python
Axon(..., glia=CARD)
Effector.serve(..., glia=CARD)
Engram.serve(..., glia=CARD)
Dendrite(..., glia=CARD)
```

Bespoke policies are declared **on the card**, not on the component, so they
travel with it:

```python
@CARD.on_tool_call
def no_writes_after_hours(tool, args): ...
```

Handler semantics copy `Effector.serve`'s settled contract exactly
(`effector/base.py:441`): handlers run in registration order, the first
non-None return answers, None falls through. Sync or async.

### 4.3 The declarative bundle

Decorators alone do not survive a fleet. With twenty components nobody can
answer "what is enforced right now" by reading twenty modules. So a card is
primarily a **versioned artifact**: content-addressed, parsed at load, and
reporting its version on REGISTER so an audit record can be attributed to a
policy version.

Decorators cover what the artifact cannot express. Both are evaluated; the
artifact first.

---

## 5. Modes

One setting, three values, default `off`:

| Mode | Behaviour |
|---|---|
| `off` | No checkpoint runs. The default, so existing brains are unaffected. |
| `audit` | Checkpoints run, verdicts are recorded, **nothing is blocked**. A deny records `verdict: "would_deny"` on the allowed signal. |
| `enforce` | Verdicts are applied. |

`audit` is the migration path and is not optional to implement: turning
`enforce` on without a shadow period is a guess.

A policy handler that itself raises **fails closed** in `enforce`, with an
explicit per-card `fail_open` opt-out. Either way the failure is recorded.

---

## 6. The five checkpoints

Three caller-side in the Axon, two callee-side in the component that owns the
effect.

### Caller side, `cosmonapse/axon.py`

| # | Checkpoint | Call site |
|---|---|---|
| 1 | `on_input` | `_handle_task_inner`, immediately **before** `_apply_before_task` (~line 636) |
| 2 | `on_output` | after recognisers, before the `agent_output_signal(...)` return (~line 731) |
| 3 | `on_tool_call` | `_run_tool_call`, beside the existing `validate_args` check (~line 979) |

Checkpoint 1 runs **before** the `before_task` hooks so policy sees what
actually arrived, not what the developer's prompt builder reshaped. Also strip
"or per-Axon policy checks" from `before_task`'s docstring (~line 469) and
point it here.

### Callee side

| # | Checkpoint | Where |
|---|---|---|
| 4 | `on_tool_call` | before `Effector.invoke` |
| 5 | `on_recall` / `on_imprint` | before `Engram.recall` / `Engram.imprint` |

`Effector.invoke` (`effector/base.py:351`), `Engram.recall`
(`engram/base.py:351`) and `Engram.imprint` (`engram/base.py:365`) are all
`@abstractmethod`, so the check cannot be wrapped inside them. Add concrete
wrappers on the ABCs:

- `Effector.guarded_invoke(...)` calls the card, then `self.invoke(...)`
- `Engram.guarded_recall(...)`, `Engram.guarded_imprint(...)` likewise

Then change the three servicing branches to call the wrapper:
`_on_tool_call` (`dendrite.py:3401`, invoke at ~3420), `_on_recall`
(`dendrite.py:3278`), `_on_imprint` (`dendrite.py:3325`). With no card the
wrapper delegates straight through, so behaviour is identical.

The ABCs have no `__init__`; hold the card as a class-level default that
`Effector.serve(glia=...)` / `attach_effector` set.

**Known and accepted limitation, document it, do not fix it:** code that calls
`effector.invoke()` directly bypasses the check. And `TOOL_CALL` is not
role-gated, so `dendrite.emit_tool_call` (`dendrite.py:2094`, which goes
through `emit()` at ~2473) bypasses checkpoints 1 to 3 entirely. Checkpoints 4
and 5 are the security boundary; 1 to 3 are the developer-experience one.

### Cost when absent

Every checkpoint sits behind a null check, matching the existing
`if self._before_task_hooks` and the `if not any(rec.values())` early-out at
`axon.py:511`. An Axon with no card must do zero extra work.

---

## 7. Verdict semantics

| Checkpoint | deny | redact | escalate |
|---|---|---|---|
| 1 Axon input | feed the repair loop; if exhausted, `AGENT_OUTPUT` carrying the refusal. Neuron never runs. | input replaced, neuron runs on the copy | `PERMISSION` instead of running |
| 2 Axon output | feed the repair loop; if exhausted, `AGENT_OUTPUT` carrying the refusal | payload rewritten, emitted | `PERMISSION` carrying the pending output |
| 3 Axon tool call | `out["error"] = reason`, the existing `validate_args` shape | args rewritten before dispatch | `PERMISSION` |
| 4 Effector | `TOOL_RESULT` with `error`, never `ERROR` | result rewritten on the way out | `TOOL_RESULT` error plus `PERMISSION` on the trace |
| 5 Engram | `RECALLED` / `IMPRINTED` with `error` | rows filtered, entry masked | not offered |

`escalate` needs no new machinery. `permission_signal` (`envelope.py:435`) and
`respond_to_permission` (`dendrite.py:2277`) already carry the verdict back as
a new TASK parented to the PERMISSION, on the original trace. Today only a
cooperative Neuron can open that channel by returning a `__permission__`
marker; this lets the runtime open it.

One deny has nowhere to ride: a failed signature on an inbound signal
(Phase 5). There is no trustworthy party to answer and replying to forged
traffic is an amplification vector. Local journal and a counter only.

---

## 8. The repair loop

**This is the highest-value piece.** One loop serves three callers: an
unclaimed output, a tool call whose arguments failed validation, and a policy
refusal. It is already a planned 0.2.0 item (`output_retries` /
`InvalidOutput`) and nothing like it exists: grep confirms neither name
appears anywhere in `cosmonapse/`, and `neuron_fn` is called exactly once per
TASK (`axon.py:646` and `axon.py:650`, two branches of one pass).

### 8.1 Two retry scopes, kept distinct

**Scope 1, brain-wide, at the source.** The existing `RetryStrategy` /
`dispatch_and_wait(retry=)` / `run_with_retry`. Re-dispatches the whole
workflow, new trace by default, STOPs the abandoned attempt, optionally saga
rolls back its Engram writes. Do not extend it for policy.

**Scope 2, internal to a component.** Two flavours that must not share a knob:

- **Correction repair. Axon only**, because only the Axon holds a generator it
  can re-ask. The input *changes* between attempts. Triggers: unclaimed
  output, invalid tool args, policy refusal at checkpoint 1, 2 or 3. Each
  attempt costs a model call.
- **Transient repair. Effector and Engram.** Nobody to re-ask, so the input is
  *identical* and the hope is the world changed: dropped connection, 429, lock
  contention. Bounded by attempts and backoff.

**A policy refusal feeds correction only, never transient.** Retrying an
identical request against a deterministic policy is N guaranteed denials.

### 8.2 Prerequisite: unclaimed output must be detectable

`_apply_recognisers` (`axon.py:507`) returns the raw output both when nothing
matched and when the output is a fine plain answer. The loop cannot trigger
until those are distinguishable. Add an opt-in strict mode on the Axon where
an unclaimed output raises `InvalidOutput` instead of passing through.

Today an unrecognised tool call is silently worse than that:
`extract_tool_calls` returns `[]` on no match
(`effector/standards.py:541`), the raw output falls through, and the model's
attempted call becomes the `AGENT_OUTPUT` answer. That is the failure the text
parsers took four rounds of hardening to reduce. Once a miss costs one more
round of prompting, the parsers can afford to be *stricter* rather than more
permissive, which is what you want from something that must never misfire on
ordinary JSON.

### 8.3 Deadline nesting

Inner attempts multiplied by their cost must stay under the caller's
`timeout_s`, and under `DEFAULT_TOOL_DEADLINE_MS` (30s, `axon.py:85`) per tool
call within them. Exceed it and scope 1 times out mid-repair, re-dispatches on
a fresh trace, and STOPs an attempt that was about to succeed.

**Nothing checks this today. Add a warning at construction.**

### 8.4 Loop containment

`meta.glia.attempt` increments on each refusal within the trace. Past the
card's limit, stop offering a correctable refusal and emit a real `ERROR` with
`recoverable=False`. That is the one legitimate `ERROR` here: the loop is
broken, not the request. Because it is not recoverable, `default_retry_on`
(`retry.py:~36`) will not re-dispatch it either.

### 8.5 STOP already works

`_on_task` (`dendrite.py:2552`) registers the neuron coroutine as a child task
under the trace (`dendrite.py:2600`), so a STOP cancels it cleanly even
mid-repair. The loop inherits this. Do not break it: keep the loop inside that
same awaited child.

---

## 9. Audit

### 9.1 Two records, different questions

**On the bus, thin.** `meta.glia` on the signal the verdict shaped, carrying
verdict, policy id, policy version, checkpoint, component identity, and a
hash. Prism reads it and renders a badge, the same way it synthesizes receptor
nodes from `meta.receptor`. Pass records stay minimal: version plus which
policies fired. Sample them per card config; auditing every allow in full
inflates `meta` on every signal in the system.

**Local, full.** The component's own append-only journal, configured by the
card, carrying the matched content. A collector ships it to wherever
compliance lives. It never crosses the Synapse.

### 9.2 Repair attempts

The repair runs before anything is published, so Prism cannot see it unless
the Axon puts it on the wire.

**On the reply: a summary.** The emitted signal carries
`meta.glia.attempts`, a capped list with attempt number, kind, audit
domain, outcome and duration per entry. The policy id, never the model's
rejected output. This is the authoritative record: it is correlated by
`parent_id`, it always arrives, and no peer drops it.

**On its own subject: `AUDIT`.** One record per policy or repair event,
emitted AT the event. Carries the audit category (`kind`) and the domain it
rolls up to (`domain`), so one stream slices into a security review, an eval
review, a tool-contract review and a reliability review. `AUDIT` is in
`SYNAPSE_TYPES` and `PATHWAY_TYPES` but in none of `_TERMINAL_TYPES`,
`_WAIT_TYPES` or `_SCOPE_TERMINAL_TYPES`, so it resolves no `wait()`, closes
no Pathway, and a terminal-scoped Pathway drops it. It also counts as nothing
against a trace limit, or a trace at its cap would spend its remaining budget
on records of being at its cap and then go silent.

**Emitted at the event, not at the next attempt's start.** The earlier draft
put the record at the start of the following attempt so a hang would read as
"attempt 2 started, never finished". That property survives - a `re_asked`
record with nothing after it says the same thing - and emitting at the event
fixes what the other ordering got wrong: the count now equals the event count,
the final refusal that exhausts the loop gets a record, and a refusal with
`max_attempts: 0` gets one too. Both of those emitted nothing before, which
broke the one inference an operator actually wants: **no `AUDIT` on a trace
means nothing fired.**

**Which is why the stream is on by default** once a card is in `audit` or
`enforce`. Absence is only meaningful if presence is guaranteed.
`repair.emit_audit: false` turns it off for a high-traffic card whose
redactions would double its signal volume; the verdict still rides the reply.

**Failure paths carry the record too.** Attempts exhausted: the terminal
`ERROR` carries the list. STOP mid-repair: the `STOPPED` ack carries what
happened so far. Otherwise the cases most worth auditing emit nothing.

The same summary treatment applies to Effector and Engram transient retries,
riding the `TOOL_RESULT` or `IMPRINTED`.

---

## 10. Trace-wide limits: the Dendrite, not the card

A card sees only its own component. Trace totals belong on the Dendrite, which
already receives the whole trace's traffic: `PATHWAY_TYPES` is every signal
type except TASK, REGISTER, DEREGISTER, HEARTBEAT and DISCOVER, and
`_ensure_pathway_subs` (`dendrite.py:2004`) subscribes to all of them.

Enforcement is a local refusal or `stop_trace` (`dendrite.py:2754`), which
already does cooperative cancellation with saga rollback.

| Mode | Guarantee | Cost |
|---|---|---|
| `component` | **exact** | Counts only what this Dendrite emitted on the trace. No subscription. **Default.** Catches the common runaway, which is one component spinning. |
| `trace` | **approximate for peers, exact for itself** | Also counts everything seen, so a peer's actions on the trace count. Needs the broadcast subscription. Soft by at most one action per concurrent PEER, since two Dendrites can each check and act before either sees the other. Its own actions are exact. |
| `trace_strict` | exact | Needs one authority for the count, so a round trip per decision. Implement last, or not at all until someone needs a hard spend cap. |

**Do not name a mode `exact` if it is only exact when nothing is concurrent.**

**Corrected 2026-09-17, after measurement.** `trace` mode was built counting
only what ARRIVES, which does not bound a burst at all. A Synapse delivers
asynchronously, so ten emits with no await in between cleared every pre-check
while the count was still zero: on a cap of three, all ten got through, while
`component` mode held at three. Since a spinning component is exactly a tight
loop, the mode failed hardest at the one runaway section 10 says it exists
for. Both modes now count on the way OUT, and `trace` additionally counts
what it sees, with the signal id deduplicating its own loopback. A peer's
actions stay approximate, which is inherent and is why the mode is still not
called exact.

The gap was found by writing the Phase 4 accept test that the work order
already asked for and that had never been written. Both of its halves are now
asserted, including the "no subscription" half, which the test claimed in its
name and left unchecked.

An earlier draft proposed an attenuated budget token carried in `meta` and
re-signed per hop. **It is dropped.** It bought exactness at the price of
signing, delegation semantics and a fan-out split rule.

Subscription cost is a property of the pub/sub model: the subject is
`cosmonapse.<namespace>.<TYPE>` (`dendrite.py:2519`), so subscribers receive
every trace and filter client side in `_dispatch_inbound`
(`dendrite.py:2990`), meaning every Dendrite decodes every signal in the
namespace. Putting the trace in the subject would move the filter to the
broker, and the convention already takes a suffix (`_routed_subject`,
`dendrite.py:2522`). If that is ever done: NATS handles high-cardinality
subjects, Kafka does not, so on Kafka the trace is a partition key and
broker-side trace filtering is unavailable. Out of scope here.

---

## 11. Identity and signing (Phase 5)

Until an envelope carries a signature, any participant can publish as any
`directed.id`, and `_decide_permission` publishes through `_publish`
(`dendrite.py:2499`), so any Dendrite of any role can answer a PERMISSION.
**Policy without identity is a developer convenience, not governance.** This
phase is what makes the governance claim true.

- Component identity is the fingerprint of the card's public key, so claiming
  another component's `directed.id` requires its private key.
- The Dendrite is the card reader: it signs each outbound signal with the card
  of the participant that produced it, and verifies each inbound one against
  its trust set before anything else touches it.
- Verification lives in the Dendrite's own handlers. `MessageHandler` is
  `Callable[[Signal], Awaitable[None]]` (`synapse/base.py`), so the Dendrite
  never sees the wire bytes. **The `Synapse` ABC does not change.**
- **Therefore the signed bytes must be a canonical form derived from the
  model, not from the transport.** `Signal.encode()` is not canonical:
  `payload` and `meta` key order comes from insertion, and `MemorySynapse`
  never serialises at all. Write an explicit canonicaliser: sorted keys, fixed
  separators, defined datetime representation. **A bug here fails silently
  rather than loudly. Test it adversarially.**
- Rollout order: sign everywhere while in `audit`, then flip to `enforce`.
  Flipping first rejects legitimate traffic from anything not yet carded.
- A mixed-version namespace is a degraded namespace: an older SDK ignores
  `meta` and therefore accepts forged signals. State this, do not paper it.

### 11.1 Revocation: no card lifetime

**Decided: cards do not expire.** Nothing phones home, nothing expires
mid-flight, no component fails closed because the management plane was
unreachable.

Revocation is therefore operational: observability alerts the developers, they
stop the brain and reissue. Time to cut off a component equals detection plus
a human deciding plus a restart, and a compromised component keeps a validly
signing identity for all of it. That cost is accepted.

Two things make it workable:

- **Issuer root rotation is the emergency lever.** Rotate the root, reissue,
  and every old card is invalid at once. Needs a restart anyway.
- **Card age is telemetry.** The card carries `issued_at` and REGISTER carries
  the card and policy version, so the fleet view can show a component running
  a six-week-old card.

---

### 11.2 Where the gap actually is (parked, not dropped)

The missing authentication is at **Dendrite to Synapse**, nowhere else.
`Dendrite` to Axon/Effector/Engram is an in-process method call with no
boundary to defend. `Synapse.publish(subject, signal)` and
`subscribe(subject, handler)` carry no publisher, so a receiving Dendrite has
only the payload's own claims, and `directed.id` is one of those claims. In a
single-process brain on `MemorySynapse` there is no exposure at all; it
appears only with multiple processes on a shared broker.

Broker credentials do not solve it. NATS accounts and TLS answer "may this
process connect"; signing answers "did the component named in this signal
produce it". Twelve components sharing one credential are fully authenticated
at the connection layer and wholly unauthenticated at the signal layer.

What is currently possible for anything on the bus, all verified in the tree:

1. **Self-grant an escalation.** `_decide_permission` publishes through
   `_publish`, so no gate applies, and `await_decision` correlates on
   `parent_id` and takes the first answer. Any participant can approve a
   PERMISSION raised by a policy. This defeats the `escalate` verdict.
2. **Forge a `TOOL_RESULT`.** Goes through `emit()`, which gates only TASK and
   STOP. The caller correlates on parent and `call_id`, so fabricated tool
   output can beat the real Effector. No policy fires, because nothing was
   blocked; the data was falsified.
3. **Forge a `RECALLED`.** Prompt injection through the channel the agent
   trusts most.
4. **Impersonate a Neuron.** `AGENT_OUTPUT` with someone else's
   `directed.id` fires their `@axon.host.on_agent_output(neuron=...)` chain
   handlers.
5. **Claim any role.** `_require_orchestrator` checks `self._role`, set in the
   component's own constructor. The role gate prevents developer mistakes, not
   attacks. Keep it for compatibility; do not mistake it for a control.
6. **Replay.** No nonce and no freshness window.
7. **Read everything.** Subscription is by subject and `cosmonapse.>` is a
   documented wildcard.

Realistic threats in order of likelihood: a prompt-injected or buggy neuron in
your own brain reaching `axon.dendrite`; a third-party Axon or Effector; two
teams sharing one namespace. Case one is the common one and is what the
callee-side checkpoints already handle today, because the Effector refuses for
itself whatever the caller claimed to be.

**Requirement for whoever builds Phase 5, and it is not derivable from "sign
the envelope":** signature validity is not sufficient. A reply must be checked
against the identity it was addressed to. A `TOOL_RESULT` must be signed by
the Effector the call went to, a `RECALLED` by the addressed Engram, and a
`PERMISSION_DECISION` by a card permitted to decide. Without that check the
system moves from "anyone can forge" to "any carded component can answer for
any other". The correlation tables in `EffectorClient`, `EngramClient` and
`await_decision` are where the expected-signer check goes.

---

## 12. Do not do these

Each one was considered and rejected for a reason recorded here.

1. **Do not express a policy deny as `ERROR`.** `_AUTO_CLOSE` /
   `_TERMINAL_TYPES` closes the Pathway on `ERROR` unconditionally and
   `recoverable` does not spare it (`pathway.py:62`). Worse,
   `default_retry_on` retries an `ERROR` flagged `recoverable`, and
   `RetryStrategy.new_trace` defaults to `True`, which STOPs the abandoned
   attempt and can saga-roll-back its Engram writes. A policy deny as a
   recoverable ERROR means three guaranteed denials, three STOPs and three
   rollbacks.
2. **Do not add a `POLICY_DECISION` signal type.** See constraint 1.
3. **Do not add a `WARNING` signal type.** Same reason. `meta.glia` on the
   refusal covers it.
4. **Do not push violations to a Prism endpoint.** Prism observes the
   Synapse. An ingest endpoint makes every component depend on a reachable
   observer, which is a centralised coupling in a decentralised design.
5. **Do not put the counter for trace limits in the management plane.** A
   policy is a static artifact you hand out in advance; a count is mutable
   shared state that changes per call. The moment a component asks the plane
   for a count, the plane is in the request path.
6. **Do not build a central policy decision process.** If someone needs real
   interception in one place, the answer is a mediator Effector in the path (a
   proxy that verifies then forwards), which is already legal. An observing
   Dendrite cannot intercept: the servicing branch deliberately does not
   consume `TOOL_CALL`, so the broker fans it to the Effector and the observer
   concurrently, and a STOP arrives after the effect.
7. **Do not ship classifiers.** Cosmonapse ships the seam, not a PII regex
   library or an injection detector. "cosmonapse does not build your tools"
   applies here too. Note in the docs that an empty card does nothing, so
   nobody expects out-of-the-box protection.
8. **Do not mutate a signal to redact it.** See constraint 7 in section 3.

---

## 13. Work order

Each phase ends green with the existing suite untouched.

**Phase 0. Seam only.**
`cosmonapse/glia/` with `Glia`, `Verdict`, the five checkpoint
decorators, the three modes, and the journal. `glia=None` kwargs on Axon,
`Effector.serve`, `Engram.serve`, `Dendrite`. No checkpoint wired yet.
*Accept:* importing does nothing; full suite green; public introspection
returns the checkpoint and verdict vocabulary.

**Phase 1. Caller-side checkpoints.**
Checkpoints 1 to 3 in `axon.py`, `meta.glia` on the shaped signal, `audit`
and `enforce` modes, `before_task` docstring fixed.
*Accept:* a card that denies a tool call produces the same observation shape
as `validate_args` does today; `audit` mode blocks nothing; an Axon with no
card executes zero extra branches.

**Phase 2. Callee-side checkpoints.**
`guarded_invoke` / `guarded_recall` / `guarded_imprint` on the ABCs, three
servicing branches switched to the wrappers.
*Accept:* a refused tool call answers `TOOL_RESULT` with `error` and the
parent TASK survives; a hand-rolled `Effector` subclass needs no changes.

**Phase 3. The repair loop.** (Highest value. Could lead instead of Phase 2.)
Strict mode and `InvalidOutput`, the correction loop in the Axon, transient
retry for Effector and Engram, the attempt counter, the construction-time
deadline warning, `meta.glia.attempts`, and the `AUDIT` audit stream.
*Accept:* an unrecognised tool call is repaired rather than shipped as the
answer; a STOP mid-repair cancels cleanly and the `STOPPED` ack carries the
attempts so far; scope 1 and scope 2 attempt counters are separately visible.

**Phase 4. Trace limits.**
`component` mode, then `trace`.
*Accept:* a looping component is refused locally with no subscription; `trace`
mode refuses within one action of the limit under concurrency.

**Phase 5. Identity and signing.**
Canonicaliser, keypair, sign on emit, verify on inbound, trust set, issuer
root rotation.
*Accept:* a signal with a forged `directed.id` is dropped before servicing and
never answered; adversarial canonicaliser tests pass; `enforce` after a full
`audit` rollout rejects nothing legitimate.

**Phase 6. Genesis and Prism.**
Prism renders `meta.glia` badges and attempt lists, and aggregates repair
rate per neuron, per trigger, per model. Genesis renders a card editor from
public introspection.
*Note:* Prism metrics cover time, structure and outcome, and an attempt is all
three, so this needs no new metrics concept. It would be Prism's first eval
metric.

---

## 14. Open decisions: ask, do not invent

**Settled 2026-09-17:**

- ~~Whether `fail_open` is exposed per policy or only per card.~~ **Per card
  only.** Not for simplicity: evaluation stops at the first answer, so a
  raise leaves the rest of the chain unreached, and a per-policy flag is
  defined only for the policy that happened to fail rather than for the one
  that would have decided. Fail closed stays the default.
- ~~The exact `meta.glia` field names.~~ **Settled as constants in
  `glia/base.py`** (`F_VERDICT`, `F_POLICY_ID`, `F_POLICY_VERSION`,
  `F_CARD_ID`, `F_CHECKPOINT`, `F_COMPONENT`, `F_HASH`, `F_MODE`,
  `F_ATTEMPT`, `F_ATTEMPTS`, `F_LIMIT`), with `BUS_FIELDS` as the allowlist
  that `_bus_record` checks itself against.
- ~~Whether `trace_strict` is built at all.~~ **Not built.** Refused at card
  load with the reason, and named under `describe()["not_built"]` so a card
  editor shows the gap rather than inventing the field.
- ~~The journal format and whether a collector ships in core.~~ **JSONL,
  append only, no collector.** A collector in core would make every
  component depend on a reachable endpoint, which is the centralised
  coupling section 12.4 rejects. Shipping the file is the deployment's job,
  and the accepted cost is that an unshipped journal dies with its box.

**Still open:**

- The name of the management plane. Genesis builds, Prism observes, this one
  governs; it takes a tool name, not an anatomy word. (The card primitive is
  `Glia`, per section 0; this is the plane that issues and distributes
  cards, not the card.)

---

## 15. Related

- `design/DECISIONS.md` for the OSS versus control-plane rule. Enforcement,
  the verdict type and `meta.glia` are Apache in core. The product is card
  issuance, policy distribution, the fleet view and the audit sink. Delete the
  control plane and every card still works.
- `design/ENVELOPE_SPEC.md` section 2 for why `meta` is the safe carrier and a
  new type is not.
- `design/RECEPTOR_DESIGN.md` and `design/ENGRAM_DESIGN.md` for the house
  shape this document follows.

---

## 16. Implementation log, 2026-09-17

Phases 0 to 4 built in one session, against this document. Recorded here
because several of the decisions below are not derivable from the sections
above: the document underdetermined them, or measurement contradicted them.
Where a section was superseded, the correction sits in that section and this
log points at it rather than restating it.

### 16.1 What was built

**Phase 0.** New package `cosmonapse/glia/`: `base.py` (Glia, Verdict,
Decision, Checkpoint, Mode, the five checkpoint decorators, the `meta.glia`
field constants with `BUS_FIELDS` as their allowlist, RepairPolicy,
TraceLimits, TraceCounter, AuditKind, AuditOutcome), `artifact.py`
(PolicyArtifact, PolicyRule, the JSON bundle's parser and validator),
`journal.py` (Journal, append-only JSONL), `__init__.py` (47 exports).
`glia=` kwargs on `Axon`, `Effector.serve`, `Engram.serve` and `Dendrite`,
plus `attach_axon`, `attach_engram`, `attach_effector` and `add_axon` for
deployments that hand out cards where components are wired rather than where
they are constructed. REGISTER announces the card through `register_meta`,
so card age is telemetry per section 11.1.

**Phase 1.** Checkpoints 1 to 3 in `axon.py` at the sites section 6 named,
each behind a null check on the card. `before_task`'s docstring no longer
advertises itself as the place for policy checks and points here instead.

**Phase 2.** `Effector.guarded_invoke`, `Engram.guarded_recall` and
`Engram.guarded_imprint` as concrete methods on the ABCs with a class-level
card default, and the three servicing branches switched to them. One
additive envelope change was needed: `recalled_signal` gained an `error`
kwarg, because section 7 requires a denied RECALL to answer `RECALLED` with
`error` and only `imprinted_signal` had the field. A payload field, not a
`Signal` change, so constraint 2 holds.

**Phase 3.** `InvalidOutput` and the opt-in `strict_output`;
`_apply_recognisers` now returns `(value, claimed)`, which is the section 8.2
prerequisite; the correction loop wrapped around a new `_one_pass`;
`@axon.repairs`; per-trace repair records with `forget_trace`; transient
retry for Effector and Engram through `run_with_transient_retry`; the
construction-time deadline warning; `repair.deadline_s`;
`meta.glia.attempts`; and the STOPPED ack carrying the attempts so far.

**Phase 4.** `TraceCounter`, `component` and `trace` modes,
`TraceLimitExceeded`, and the LIMIT_REFUSED audit record. See the correction
in section 10.

### 16.2 What the document underdetermined, and what was chosen

**Checkpoint 1 deny has no loop.** Section 7 gives its deny cell both "feed
the repair loop" and "Neuron never runs". Those conflict: at checkpoint 1
there is no generator to re-ask, because the input arrived from outside and
no attempt of ours changes what arrived. So a checkpoint 1 deny refuses
immediately, and section 8.4's terminal `ERROR` with `recoverable=False`
fires when a NEW TASK arrives on a trace whose repair count is already past
the limit. That reading gives 8.4's ERROR a real trigger and keeps 7's
"Neuron never runs" literally true.

**How the input changes between attempts.** Section 8.1 requires it and does
not say how. `@axon.repairs` owns the next attempt's input; without one, the
default builder merges a `repair` key, which reaches the Neuron only if a
`before_task` prompt builder or the Neuron itself reads it. Because a
provider that ignores it would otherwise burn model calls on an identical
request, the loop compares the built input against the previous one and stops
on the first attempt when nothing changed, recording `uncorrectable`. That is
the same rule that keeps a policy refusal out of transient retry, applied to
a no-op builder.

**`escalate` at the Engram checkpoints.** Section 7 says "not offered" and
does not say how. A rule declaring it at `on_recall` or `on_imprint` is
refused at card load with the reason, and a handler returning it there is
treated as the policy error it is and fails closed rather than silently
allowing the effect.

**Recalled-row masking.** Section 7 assigns "rows filtered, entry masked" to
the `on_recall` redact verdict, but section 6 puts that checkpoint before the
call. So `check_recall` governs the query and `redact_rows` runs after,
masking string leaves, dropping keys, and dropping a row left hollow. Its
record merges into the reply's, because otherwise the one thing the policy
actually did would go unrecorded.

**An explicit pass, not an inferred one.** `sample_pass: 1` makes a clean
turn carry `meta.glia.verdict = "allow"`, so a display reads pass from the
reply rather than from the absence of a record on a best-effort bus.

### 16.3 What measurement contradicted

**Constraint 1's blast radius was smaller than stated**, which is what made
the `AUDIT` override defensible. Every Synapse adapter already wraps
`Signal.decode` in a try and continues (`nats.py:127`, `kafka.py:222`,
`dev.py:510`), so an SDK that predates a member logs one warning per signal
and drops it. Nothing crashes and no subscription dies. The override, and
the cost that remains, are recorded in constraint 1 itself.

**Section 10's `trace` mode did not bound a burst at all.** Measured, not
reasoned: on a cap of three, ten emits with no await in between all got
through, because counting only what arrives leaves every pre-check passing
while delivery is still queued. The fix and the numbers are in section 10.

**Section 9.2's "emit at the attempt's START" was superseded.** Emitting at
the event instead makes the record count equal the event count, which is what
lets the absence of any AUDIT on a trace mean nothing fired. The reasoning is
in 9.2.

**There are 25 pre-existing test files, not 26.** Constraint 6's intent held
regardless: see 16.4.

### 16.4 Constraint 6 was broken once, deliberately

Constraint 6 says the existing test files must pass untouched, and that if
one needs editing the edit is the disruption and needs a decision rather
than a workaround. One did. Removing the `THOUGHT_DELTA` signal type forced
a single parametrize row in `tests/test_cognition_api.py` to change from
`on_thought_delta` to `on_retry`, later `on_audit`. It was raised and
decided rather than worked around. No other pre-existing test file was
touched.

The constraint earned its keep twice in the other direction. A rename sweep
of `on_retry` to `on_audit` also caught `RetryStrategy.on_retry` and
`rollback_on_retry` inside `Dendrite.run_with_retry`, silently breaking
scope 1 re-dispatch; `tests/test_stop_retry_saga.py`, untouched, failed
immediately. The Glia rename was then guarded with a programmatic check of
eighteen assertions over the names that had to survive, rather than by
eyeballing a diff.

### 16.5 Verification

633 tests pass, 8 skipped. 4 new test files under
`tests/test_glia_{card,checkpoints,repair,audit}.py`, alongside the 25
pre-existing ones. `ruff check .` clean against the tree's explicit select
list. `mypy --strict` clean on 44 source files. `prism-ui` typechecks with
`tsc --noEmit`.

Two notes on how that was run. The suite is executed from a fresh copy of
the committed tree rather than from a working copy, so what passes is what
is on disk. And the local Linux VM ships Python 3.10 while the project
requires 3.11, so runs go through a `sitecustomize` shim supplying
`datetime.UTC` and `typing.Self`; `npm` will not complete inside the
OneDrive-synced checkout, so `prism-ui` is typechecked from a copy outside
it.

### 16.6 Repo state, not design

Three things a reader will hit that are not this design's doing. The frozen
`cosmo/commands/prism_dist/assets/prism.js` bundle still carries the
pre-rename compiled names and needs `build:into-wheel` re-run from outside
the OneDrive tree. `git status` reports every `python-sdk` file as modified
because the worktree is CRLF while the index is LF with `core.autocrlf`
unset, so a content diff needs `--ignore-cr-at-eol`. And
`design/.fuse_hidden0000001700000001` is a tracked stale FUSE copy of an
older ENVELOPE_SPEC, committed in `b2a33c2 lexicon`, still in the repo.

---

## 17. Glia as a two-way gate on the component (built 2026-09-23)

**Status: built.** Supersedes sections 2, 4.2 and 5 to 7 wherever they
talk about checkpoints, and section 8 wherever it says a policy refusal
always feeds the repair loop. Signing stays deferred (section 11).

### 17.1 The model

Glia is a thin gate wrapped around one component: an Axon, an Effector or
an Engram. It reads every signal going into that component and every
signal coming out of it, and enforces the card's policies on them. There
are no checkpoints. A checkpoint was only ever a scope: "this policy
applies to TASK arriving", "this policy applies to TOOL_CALL leaving".
Scope is now a field on the policy, and its default is everything.

Every policy has:

- `direction`: `inbound`, `outbound` or `both`. Default `both`.
- `types`: the signal types it applies to, or `["*"]`. Default all types.
  Narrowing is optional: `["TOOL_CALL"]` is what `on_tool_call` used to
  mean.
- a matcher (`match`, `keys`, `always`, or a decorator handler) over the
  signal's payload, and a verdict: `allow`, `deny`, `redact` or
  `escalate`. `tools`, `ops` and `components` still narrow further.
- `retry`: what a violation does next. Default `none`. See 17.3.
- `max_attempts`: that policy's retry budget. Default 2.

```json
{"id": "no-secrets-into-memory", "direction": "outbound",
 "types": ["IMPRINT"], "verdict": "deny",
 "match": ["sk-[A-Za-z0-9]{16,}"], "retry": "reask", "max_attempts": 2}

{"id": "no-pii-either-way", "verdict": "redact", "keys": ["ssn"]}

{"id": "recalled-must-not-carry-instructions", "direction": "inbound",
 "types": ["RECALLED"], "verdict": "deny",
 "match": ["(?i)ignore (all|previous) instructions"], "retry": "resend"}
```

A decorator is the same thing in code:
`@CARD.on_signal(direction="inbound", types=["RECALLED"], retry="resend")`.
`@CARD.on_signal` with no arguments reads everything, both ways. A card
that still says `"checkpoint"` is refused at load with the mapping.

### 17.2 What each component's gate sees

| Component | Inbound | Outbound |
|---|---|---|
| Axon | `TASK`; the replies to its own calls: `TOOL_RESULT`, `RECALLED`, `IMPRINTED` | `AGENT_OUTPUT`, `CLARIFICATION`, `PERMISSION`, `ERROR`; its own calls: `TOOL_CALL`, `RECALL`, `IMPRINT` |
| Effector | `TOOL_CALL` | `TOOL_RESULT` |
| Engram | `RECALL`, `IMPRINT` | `RECALLED`, `IMPRINTED` |

A permission decision reaches an Axon as a new `TASK`, so the inbound
`TASK` scope covers it.

**The Dendrite has no policy role.** It routes and publishes as it did.
The mechanics that let the gate sit on the component side:

1. **Servicing moved into the component.** `Effector.handle(signal)` and
   `Engram.handle(signal)` return the reply to publish, and replace
   `guarded_invoke`, `guarded_recall` and `guarded_imprint`. The
   Dendrite's `_on_tool_call`, `_on_recall` and `_on_imprint` call
   `handle` and publish what it returns. Both go through one helper,
   `serve_gated`, which reads the request inbound and the reply outbound.
2. **The Axon's own calls go through its gate.** For the length of one
   TASK the Axon binds a `CallerGate` in a context variable. The
   `EffectorClient` and `EngramClient` read it and hand it the request
   before publishing and the reply on arrival. That covers the injected
   `recall` / `imprint` / `call_tool` helpers, the native tool channel,
   and a Neuron calling `axon.dendrite.recall(...)` directly. The
   Dendrite never reads a card.
3. **Delivery clears the caller gate.** On an in-process synapse a signal
   is delivered inside its publisher's own call. `_dispatch_inbound` and
   `serve_gated` both clear the caller gate, so an Effector handler or a
   host `@on_*` handler that calls `recall` is not read as if the calling
   Axon had sent it.

**Known and accepted bypass**, as before: code that holds the Dendrite
and calls `_publish` or `emit_*` directly is not read by any Axon's gate.
The callee-side gates still read what arrives at them.

### 17.3 Violation: audit, then retry per policy

A violation is a `deny`. Every violation emits one `AUDIT` record at the
event, and then the policy's `retry` decides what happens:

| `retry` | Meaning | Where it works |
|---|---|---|
| `reask` | Re-run the Axon's Neuron with the refusal as the correction input (the section 8 correction loop) | Axon, outbound anything; Axon, inbound `TOOL_RESULT`, `RECALLED`, `IMPRINTED` |
| `resend` | Repeat the request behind the violating reply, unchanged, as a new signal | `TOOL_RESULT` and `RECALLED`, either side: the Axon resends the call, the Effector re-invokes, the Engram re-runs the recall |
| `none` | Refuse now | everywhere; the default |

- **`IMPRINTED` is not resendable.** Resending an `IMPRINT` repeats a
  write, and not every backend's writes are idempotent.
- **Refused at load:** `resend` whose `types` are not all resendable
  (which includes `resend` with no `types`), `reask` scoped to inbound
  `TASK` only, `retry` on anything but a deny, `escalate` anywhere a
  PERMISSION has nowhere to go (the allowed scopes are inbound `TASK`,
  outbound `AGENT_OUTPUT` and outbound `TOOL_CALL`).
- **Degraded at runtime:** a retry with nowhere to go becomes `none`.
  `reask` needs the Axon's repair loop, which exists only when the card's
  `repair.max_attempts` (or an explicit `repair=`) is above zero, and never
  exists on an Effector or Engram.
- **Budgets.** `resend` is bounded by the policy's `max_attempts`.
  `reask` is bounded by the lower of the policy's `max_attempts` and the
  card's `repair.max_attempts`, and `repair.deadline_s` still applies.
- **Records.** `GUARD_RETRY` for an event that is retried or whose retries
  ran out (`re_asked`, `resent`, `exhausted`). `GUARDED` for a refusal,
  redaction or escalation with no retry (`refused`, `redacted`,
  `escalated`, and `would_block` in `audit` mode). The payload carries
  `direction` and `signal` in place of the old `checkpoint`.
- **A refusal inside a Neuron.** A refused outbound call, or a refused
  reply to one, raises `PolicyRefusal` where the call was made. A Neuron
  that catches it has handled it; the violation is still recorded when
  the pass ends, so no `AUDIT` on a trace still means nothing fired. One
  that lets it propagate hands it back to the Axon, which re-asks or
  answers with the refusal. Either way it is recorded once.
- **Exhausted, or `none`:** the type-correct refusal goes out. An Axon
  answers `AGENT_OUTPUT` with `error`, `refused_by: "policy"` and the
  `scope` (for example `outbound:TOOL_CALL`); an Effector answers
  `TOOL_RESULT` with `error`; an Engram answers `RECALLED` or `IMPRINTED`
  with `error`. Constraint 3 holds: never `ERROR`, except section 8.4's
  loop-broken case.
- **Merged recall.** In `recall_mode` `merge` or `all`, a responder whose
  `RECALLED` the Axon refuses is dropped from the merge and is not
  resent, because the others already answered.
- `redact` rewrites the payload on a copy and continues. `escalate`
  raises `PERMISSION` as before. `audit` mode records `would_*` and
  neither blocks nor retries.

### 17.4 What a handler sees

```python
@CARD.on_signal(direction="inbound", types=["TOOL_RESULT"])
def only_known_effectors(sig: SignalView) -> Verdict | None:
    if sig.directed_id not in {"search", "calc"}:
        return Verdict.deny("tool result from an unknown effector")
    return None
```

`SignalView` is frozen and read-only: `type`, `id`, `trace_id`,
`parent_id`, `directed_id`, `directed_type`, `directed_capabilities`,
`ts`, `meta` (read-only copy), `payload` (copy), `direction`,
`component`. A redact verdict returns the new payload, a dict; anything
else fails closed. The envelope and `meta` are not writable by a policy.
A handler may also declare `trace_id`, `component`, `direction` or `card`
as keyword parameters.

`directed_id` is a claim. Without signing it stops mistakes and
misrouted traffic, not an attacker on the bus (section 11.2).

### 17.5 Exemptions

Two things pass every gate unread, so a card cannot silence the system
that reports on it or deny its own refusal: `AUDIT` signals, by type, and
signals a gate or the repair loop generated (refusal replies, the
PERMISSION an escalation raises, the loop-broken `ERROR`), by id. The
`STOPPED` ack is published by the Dendrite, which has no gate.

### 17.6 What stays where it is

- Trace limits (section 10) stay on the Dendrite's card. They count a
  trace, not a component, so they do not fit a per-component gate. A
  Dendrite card that carries policies logs a warning at construction,
  because it would be reported as enforced and never run.
- `strict_output` and the unclaimed-output repair stay on the Axon.
- Cost when absent: one null check per signal. A card indexes what it
  declares by `(direction, type)`, so a signal no policy selects costs a
  set lookup, and a card whose mode is `off` or that declares nothing
  binds no caller gate at all.

### 17.7 Implementation log

Built 2026-09-23 in `packages/python-sdk`.

- `glia/base.py`: `Direction`, `Retry`, `coerce_types`, `SignalView`,
  `with_payload`, `with_glia_record`, `mark_exempt` / `is_exempt`,
  `allowed_retry`, `RESENDABLE`, `REASKABLE_INBOUND`, `ESCALATABLE` as
  `(direction, type)` pairs, `Glia.on_signal`, `Glia.check`,
  `serve_gated`, `CallerGate` and `caller_gate`. `AuditKind.GUARDED` and
  `AuditOutcome.RESENT` added. `Checkpoint`, the five checkpoint
  decorators, `check_*` and `redact_rows` removed. Bus fields
  `direction`, `signal` and `retry` replace `checkpoint`.
- `glia/artifact.py`: rules carry `direction`, `types`, `retry`,
  `max_attempts`; `keys` now matches a key anywhere in the payload;
  `rows` removed, because masking recalled rows is an outbound
  `RECALLED` redact.
- `effector/base.py`, `engram/base.py`: `handle()` in place of the
  guarded wrappers. `dendrite.py`: the three servicing branches call
  `handle`; `_publish_policy_refusal` removed; `_dispatch_inbound` clears
  the caller gate; `emit_audit` takes `direction` and `signal`.
- `effector/client.py`, `engram/client.py`: read the caller gate, with
  the resend loop.
- `axon.py`: the inbound `TASK` gate before the context fetch and the
  `before_task` hooks; the outbound gate on every reply inside the repair
  loop and on the final fallback; `PolicyRefusal` from the Neuron's own
  calls handled per its retry.
- `envelope.py`: `audit_signal` takes `direction` and `signal` in place of
  `checkpoint`. `ENVELOPE_SPEC.md` updated to match.

**Tests.** The 25 pre-existing test files pass untouched. The four Glia
test files are this feature's own and were rewritten against the new
model; `test_glia_checkpoints.py` is now `test_glia_gate.py`. 681 passed,
6 skipped. `ruff check .` clean. `mypy --strict` reports the same
optional-dependency errors as before the change and nothing new.

**Behaviour that changed for existing cards.** A deny that used to feed
the repair loop at `on_output` or `on_tool_call` now needs
`"retry": "reask"`; the default is `none`. The one visible difference in
the wire record is `direction` and `signal` in place of `checkpoint`.
