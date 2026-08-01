# Cosmonapse Receptor  -  Design (Draft)

**Status:** Draft v0.1
**Last updated:** 2026-07-28
**Relates to:** `SDK_DESIGN.md` (layers), `ENVELOPE_SPEC.md` §5 (TASK), `ENGRAM_DESIGN.md` (the precedent for adding a primitive)

---

## 1. What a Receptor is

A **Receptor** is an interface. It is the edge where something outside the fabric - a person at a terminal, an HTTP client, someone talking into a microphone - touches it.

- A Receptor collects an input from a transport it owns.
- It turns that input into a **TASK**.
- It hands the resulting trace back in whichever of the three dispatch shapes the transport wants.

That is the entire primitive. It adds **no signal types, no subjects, and no wire format**. A Receptor emits exactly the TASK an orchestrator Dendrite has always emitted; `cosmo prism --tail` cannot tell a Receptor-originated trace from a hand-written `dispatch_and_wait`, except by the optional `meta.receptor` tag the Receptor stamps on for attribution.

Neurons think, Engrams remember, Effectors act, **Receptors listen**.

### Why it earns a name

Every example in `cosmonapse-examples` grew an edge by hand, and they all grew the *same* edge:

| example | edge | what it hand-rolls |
| --- | --- | --- |
| `01-quickstart/app.py` | Flask | background loop thread, one route, `dispatch_and_wait` |
| `05-orchestrator-api/app.py` | FastAPI | lifespan, body model, `TimeoutError` → 504 |
| `11-rag/app.py` | FastAPI | lifespan, two body models, a `/stats` route |
| `14-agent/app.py` | FastAPI | lifespan, `/run` + `/memory`, 504 + 500 mapping |
| `14-agent-cli/cli.py` | terminal | argparse, REPL loop, `:memory` / `:help` / `:quit`, result printer |
| `16-rag-cli/cli.py` | terminal | the same again, plus a live Signal trace view |

Roughly 700 lines across the repo, of which the part that differs between examples is: which command exists, what its input dict looks like, and how the result prints. Everything else is the same four moves - open the stack, collect input, dispatch, render.

A Receptor is that repetition named and made a primitive. It is *sugar*, in the same sense Pathway is sugar over `dispatch_task` + `on_agent_output`: opt-in, additive, and deletable without changing what crosses the wire.

---

## 2. Design principles

- **Nothing new on the wire.** No signal types, no subjects, no envelope fields. If a Receptor needed a new signal type, it would be the wrong abstraction.
- **The trio is the whole surface.** `send` / `wait` / `stream` map one-to-one onto `dispatch_task` / `dispatch_and_wait` / `dispatch_and_subscribe`. A Receptor never invents a fourth shape.
- **Caller-side, not host-side.** An Effector or Engram *services* signals and needs a host proxy and a subscription. A Receptor *originates* them and needs neither. It is the mirror image, and its structure should not be copied from `Effector.serve()`.
- **The Neuron must not learn about the edge.** A Neuron answering a CLI command and a chat turn sees the same TASK. Anything transport-specific (history, session, terminal colours) either stays on the Receptor or rides as ordinary input keys the Neuron may ignore.
- **The edge owns its own dependencies.** FastAPI is not a core dependency. `CliReceptor` needs nothing beyond the core install; `ApiReceptor` / `ChatReceptor` live behind `pip install 'cosmonapse[receptor]'` and are imported lazily so `import cosmonapse` never pulls FastAPI in.
- **Four backends, deliberately.** CLI, API, chat, and voice-as-an-add-on-to-chat. Not a plugin framework - a Receptor is ~200 lines and a fifth backend is a subclass, not a registry entry.

---

## 3. Layers

```
┌──────────────────────────────────────────────────────┐
│  outside world                                       │
│  argv / stdin  ·  HTTP request  ·  chat turn (voice) │
└───────────────────────┬──────────────────────────────┘
                        │  transport-specific collection
┌───────────────────────▼──────────────────────────────┐
│  Receptor                                            │
│    build_input()   raw       -> TASK input dict      │
│    send/ask/stream input     -> the dispatch trio    │
│    render()        Signal    -> transport value      │
└───────────────────────┬──────────────────────────────┘
                        │  dispatch_task / dispatch / dispatch_and_wait
┌───────────────────────▼──────────────────────────────┐
│  orchestrator-role Dendrite       (unchanged)        │
└───────────────────────┬──────────────────────────────┘
                        │  TASK on the Synapse
┌───────────────────────▼──────────────────────────────┐
│  worker Dendrite -> Axon -> Neuron   (unchanged)     │
└──────────────────────────────────────────────────────┘
```

The Receptor sits *above* the Dendrite, where an Effector and an Engram sit beside it. That is the structural difference and the reason it needs no `host` proxy.

---

## 4. The dispatch trio

| mode | Receptor call | Dendrite call | returns | ends when |
| --- | --- | --- | --- | --- |
| `send` | `rx.send(x)` | `dispatch_task` | the emitted TASK Signal | immediately |
| `wait` | `rx.ask(x)` | `dispatch` + `Pathway.wait` | the rendered result | first terminal Signal |
| `stream` | `rx.stream(x)` / `rx.iter_signals(x)` | `dispatch` (subscribe shape) | a live Pathway / async generator | terminal Signal or Pathway close |

`rx.receive(x, mode=...)` is the single funnel every backend calls; the three named methods are aliases.

**`wait` is implemented over `dispatch` + `wait`, not `dispatch_and_wait`.** The Receptor needs the Pathway in hand to attach `@on_signal` progress hooks, which `dispatch_and_wait` does not expose. Behaviour is identical; `retry=` is not surfaced (use the Dendrite directly if a retry strategy is wanted).

### 4.1 What "terminal" means

`Pathway.wait()` resolves on AGENT_OUTPUT / FINAL / ERROR / CLARIFICATION / PERMISSION. A streaming Receptor must additionally *stop* on those, because only FINAL and ERROR auto-close a Pathway - a plain worker trace ends on AGENT_OUTPUT and a stream that kept reading would hang until its deadline.

`Receptor.terminal_types()` therefore returns:

- the full set, normally;
- the set **minus AGENT_OUTPUT** when the Receptor dispatches with `finalize=True`, because there AGENT_OUTPUT is promoted to FINAL and is mid-flight rather than terminal.

`iter_signals(..., stop_on=frozenset())` opts out and reads until the Pathway closes on its own.

---

## 5. Python surface

### 5.1 Base class

```python
class Receptor(ABC):
    def __init__(self, *, dendrite=None, neuron=None, capabilities=None,
                 receptor_id="receptor", input_key="prompt",
                 timeout_s=60.0, scope="all", finalize=None, meta=None): ...

    # trio
    async def send(raw, **overrides)    -> Signal
    async def ask(raw, **overrides)     -> Any
    async def stream(raw, **overrides)  -> Pathway
    async def receive(raw, *, mode, **overrides)
    async def iter_signals(raw, *, timeout_s=None, stop_on=None, **overrides)

    # binding
    def bind(dendrite) -> Receptor         # late binding for ASGI apps
    @property dendrite / bound

    # shaping hooks
    @rx.on_input                 raw -> TASK input dict     (sync or async)
    @rx.on_result                terminal Signal -> value   (sync or async)
    @rx.on_failure               exception -> value         (sync or async)
    @rx.on_signal(SignalType.X)  observe intermediate Signals
```

Design notes:

- **`dendrite` is optional and late-bindable.** An ASGI app is imported before there is an event loop to connect a Synapse on. `rx.bind(orchestrator)` inside the lifespan is the supported pattern; `rx.dendrite` raises `ReceptorUnbound` with a pointed message if it is never bound.
- **Target is per-Receptor *or* per-call.** `neuron=` / `capabilities=` may be set at construction and overridden on any call, so one Receptor can front several Neurons.
- **`input_key` handles the bare-string case.** `rx.ask("hello")` becomes `{"prompt": "hello"}`; the examples use `prompt`, `goal`, `question`, `message`. A dict input passes through untouched.
- **Every hook may be sync or async.** The transport code awaits through `_maybe_await`, because a formatter is usually sync and a fetcher usually is not, and forcing either is friction for no gain.
- **Progress hooks are guarded.** An exception inside `@on_signal` is logged and swallowed. Observation must never break the trace it observes.

### 5.2 Errors

| exception | raised when |
| --- | --- |
| `ReceptorError` | the trace ended on an ERROR Signal |
| `ReceptorTimeout` | no terminal Signal inside `timeout_s` (subclasses `TimeoutError`) |
| `ReceptorUnbound` | no dendrite, or no `neuron=` / `capabilities=` target |

`@on_failure` intercepts before the raise, which is how a backend converts an exception into a transport-shaped value (an SSE `error` frame, a JSON body).

### 5.3 `CliReceptor`

A command function *returns the TASK input*. The argparse tree and the REPL are derived from its signature:

```python
@rx.command(help="ask the assistant")
def ask(prompt: str, tokens: int = 400):
    return {"prompt": prompt, "max_new_tokens": tokens}
```

- no default → positional (a `str` positional takes `nargs="+"` and is re-joined, so a multi-word request needs no quoting)
- default → `--flag`, typed from the annotation
- `bool` default → `--flag` / `store_true`
- `*args` / `**kwargs` → `TypeError` at declaration. Guessing at their CLI meaning produces worse errors later.

`local=True` marks a command that answers on the spot without dispatching - the `:memory` / `:stats` shape every CLI example already has. Global flags `--stream` / `--send` override the mode per invocation; no subcommand at all drops into the REPL, where `:name` runs a command and a bare line goes to the default command.

### 5.4 `ApiReceptor`

**One endpoint, three shapes.** The caller picks with `mode` in the body rather than the URL, because the *resource* is the same in all three cases and only the delivery differs:

```
POST /run  {"input": ..., "mode": "wait"}    -> JSON result
POST /run  {"input": ..., "mode": "send"}    -> {"accepted": true, "trace_id": ...}
POST /run  {"input": ..., "mode": "stream"}  -> text/event-stream
GET  /run/{trace_id}                         -> text/event-stream (observe_pathway)
```

- `allowed_modes` narrows what a caller may ask for; `max_timeout_s` clamps a caller-supplied deadline.
- A bare body (`{"goal": "..."}` with no `input` envelope) is accepted as the input, because that is what the existing examples' clients send.
- `rx.router` mounts into an existing app; `rx.app(setup=...)` builds a standalone one whose lifespan constructs the stack and binds the orchestrator.
- SSE frames are `event: <signal_type_lowercased>` with a JSON `data:` body, terminated by `event: done`. Failures become an `event: error` frame rather than a truncated stream, so a browser reader always sees a clean end.

### 5.5 `ChatReceptor`

Extends `ApiReceptor`. One turn in, one dispatch out - deliberately the simplest of the three.

What it adds beyond the raw trio is only what a conversation needs:

- **Per-session history**, capped at `history_turns` and passed as `history` (a `[{role, content}]` list) in the TASK input. `history_turns=0` is stateless. The session id also rides as `context_ref`.
- **Recorded after the dispatch, not before**, so `history` carries prior turns only and the current message never appears twice.
- **A served page** at `GET /` - single file, no build step, no CDN - that streams the turn over SSE.
- **Reply extraction**: `extract_text` walks `reply` / `response` / `answer` / `text` / `message` / `content` / `output` / `report` / `result` so an arbitrary Neuron's dict renders as prose without the developer writing a formatter.

### 5.6 Voice

Voice is a **client-side add-on to the chat page**, not a Python feature and not a protocol feature.

`voice=True` enables, in the served HTML only:

- `SpeechRecognition` / `webkitSpeechRecognition` behind a mic button - interim results type into the box, a final result submits the turn;
- `speechSynthesis` read-back behind a "speak" toggle.

Consequences, all of them intentional:

- no audio dependency in the SDK, and no audio bytes on the wire;
- nothing about voice in the protocol - the Receptor still only ever sees text, and the Neuron cannot tell a spoken turn from a typed one;
- graceful degradation - where `SpeechRecognition` is missing (Firefox, older Safari) the mic button hides itself and typing still works.

A server-side STT/TTS Receptor is a plausible future backend for telephony or a native app. It is out of scope here precisely because it *would* put audio on the wire and needs its own design pass.

---

## 6. What a Receptor is not

- **Not an Effector.** An Effector services TOOL_CALL, inbound. A Receptor originates TASK, outbound. Same nervous-system vocabulary, opposite direction.
- **Not a transport abstraction.** `ApiReceptor` binds FastAPI directly and `CliReceptor` binds argparse directly. An abstraction layer over web frameworks would cost more than the duplication it saves.
- **Not required.** Every existing example still dispatches by hand and still works. A Receptor is opt-in sugar.
- **Not a place for auth, rate limiting, or tenancy.** Those belong to the transport (FastAPI middleware) or to the Dendrite's namespace, not to the funnel between them.

---

## 7. Open questions

1. **TS SDK parity.** The Effector reached parity on 2026-07-17. A `Receptor` in the TS SDK has an obvious CLI/HTTP shape but the chat page would need to be shared or duplicated. Worth deciding before the surface hardens.
2. **`retry=` on `ask`.** Deliberately omitted (it lives on `dispatch_and_wait`, which the Receptor bypasses to keep the Pathway). If retries turn out to be wanted at the edge, the honest fix is a `retry=` on `dispatch` itself rather than a re-implementation inside the Receptor.
3. **CLARIFICATION / PERMISSION at the edge.** Both are terminal for `wait`, so a Receptor currently *reports* them and stops. A chat interface is the natural place to answer one (`respond_to_clarification` re-dispatches on the same trace) - that loop is designed but not built.
4. **`dispatch_offer`.** The bidding shape is not in the trio. It is a fourth dispatch shape on Dendrite and arguably a fourth Receptor mode; left out until there is a use case at an edge.
5. **Server-side voice.** See §5.6.

---

## 8. Status

Implemented in `packages/python-sdk/cosmonapse/receptor/` (`base.py`, `cli.py`, `api.py`, `chat.py`), exported from `cosmonapse`, covered by `tests/test_receptor.py` (36 tests against a real `MemorySynapse` and a real worker Axon), and demonstrated in `cosmonapse-examples/17-receptors`.
