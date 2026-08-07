<div align="center">

<img src="assets/logo.png" alt="Cosmonapse" width="320" />

# Cosmonapse

**A platform suite for event-driven AI systems - design them on a canvas, run them on an open protocol, watch them think. No orchestrator loop.**

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![PyPI](https://img.shields.io/pypi/v/cosmonapse?color=8B5CF6&label=PyPI&logo=pypi&logoColor=white)](https://pypi.org/project/cosmonapse/)
[![License](https://img.shields.io/badge/license-Apache%202.0-D946EF.svg)](LICENSE)
[![Transports](https://img.shields.io/badge/transports-in--memory%20%7C%20TCP%20%7C%20NATS%20%7C%20Kafka-7C3AED)](#scale-is-a-url-change)

[Quick Start](#quick-start) • [The Suite](#the-suite) • [Architecture](#architecture) • [Why](#why-this-exists) • [Docs](#documentation)

</div>

---

```bash
pip install cosmonapse    # the cosmo CLI ships with it
cosmo genesis             # opens the designer at 127.0.0.1:7072
```

Cosmonapse models a multi-agent system on a nervous system rather than a supervisor. Components emit **Signals** and react to Signals on one bus; nobody holds the loop. You grow a system by adding a node, not by editing a god object - and because every interaction already crossed one bus in one envelope format, tracing costs nothing.

[cosmonapse.com](https://cosmonapse.com) · [PyPI](https://pypi.org/project/cosmonapse/) · [Envelope Spec](design/ENVELOPE_SPEC.md) · [Examples](https://github.com/Cosmonapse/cosmonapse-examples) · [Roadmap](design/ROADMAP.md) · [Changelog](CHANGELOG.md)

## Why this exists

<div align="center">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/diagrams/call-stack-vs-bus-dark.svg" />
  <img src="assets/diagrams/call-stack-vs-bus-light.svg" alt="A call stack beside an event bus. On the left a supervisor calls step A, which calls step B, which calls step C, and a reply arriving late has no frame to return into. On the right the same late reply is an ordinary Signal on the bus." width="880" />
</picture>
</div>

Graph and loop frameworks ask you to declare control flow before you know what the system does: nodes, edges, branches, and a supervisor that turns the crank. That holds until something arrives you didn't draw - a tool returns late, a human answers halfway through, a second model disagrees with the first.

Real systems are concurrent and only partially ordered. Encoding one as a graph means encoding *time* as topology, and you end up maintaining a state machine larger than the problem. Cosmonapse takes the other side: concurrency, fan-out, retries and ordering are properties of the transport - the same properties that have run distributed systems for twenty years - rather than branches you drew in advance.

## The suite

<div align="center">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/diagrams/design-run-observe-dark.svg" />
  <img src="assets/diagrams/design-run-observe-light.svg" alt="Three windows: the Genesis canvas with a Synapse and four components around it, a terminal running brain.py and printing REGISTER, TASK, TOOL_CALL and FINAL, and Prism's Brain View showing the same four components with Signals travelling between them." width="880" />
</picture>
</div>

| | What it is | Start it |
| --- | --- | --- |
| **Core** | The open protocol, the Python SDK and the `cosmo` CLI. Stands on its own and always will. | `pip install cosmonapse` |
| **Genesis** | The designer. Lay a system out on a canvas; it writes real modules into your project and edits them through the AST. | `cosmo genesis` → 127.0.0.1:7072 |
| **Prism** | The observability plane. One Signal stream, five views: the live graph, a run's execution graph, the causal tree, the raw record, the numbers. | `cosmo prism` → 127.0.0.1:7071 |

They share exactly one thing - the Signal envelope. That is what lets the designer, the runtime and the observability plane stay honest about the same system without any of them owning it. Genesis and Prism both ship inside the `cosmonapse` wheel; there is nothing extra to download and no runtime dependency on either.

## Quick Start

```bash
pip install cosmonapse

cosmo init my-app -n demo
cd my-app
python brain.py
```

`cosmo init` writes a working brain, not a hello-world file:

```text
my-app/
├── brain.py            # the entry point - attaches every node, serves every interface
├── config.py           # namespace + synapse URL
├── neurons/hello.py    # a Neuron: an async function behind an Axon
├── effector/tools.py   # an Effector: a tool family
├── engram/store.py     # an Engram: memory as a protocol citizen
└── receptors/terminal.py   # a Receptor: the edge a turn arrives at
```

`python brain.py` starts every node and drops you into a REPL on the terminal Receptor. It also takes one-shot forms, which map onto the three dispatch modes:

```bash
python brain.py greet --name Ada    # dispatch_and_wait
python brain.py --stream greet      # dispatch_and_subscribe
python brain.py --send greet        # dispatch_task, fire and forget
```

New Neuron modules go under `neurons/`, tool families under `effector/`, interfaces under `receptors/`; wiring stays in `brain.py`. The same code over a real broker:

```bash
cosmo synapse start memory --namespace=demo
SYNAPSE_URL=cosmo://127.0.0.1:7070 python brain.py
```

Or skip the terminal entirely and grow it on the canvas with `cosmo genesis`.

### Written by hand

Nothing above is required. A brain is two objects and a function:

```python
import asyncio
from cosmonapse import Axon, Dendrite, MemoryRegistryStore, connect_synapse

async def main():
    synapse = await connect_synapse("cosmo://127.0.0.1:7070")
    try:
        async def answerer(input, context):
            return {"answer": input["q"]}

        worker = Dendrite(synapse=synapse, namespace="demo")
        worker.attach_axon(Axon(neuron_id="answerer", neuron_fn=answerer))

        orch = Dendrite(synapse=synapse, registry_store=MemoryRegistryStore(),
                        namespace="demo")

        @orch.on_agent_output
        async def done(sig):
            await orch.emit_final(trace_id=sig.trace_id, parent_id=sig.id,
                                  result=sig.payload["output"])

        async with orch, worker:
            await orch.dispatch_task(neuron="answerer", input={"q": "hi"})
            await asyncio.sleep(0.5)
    finally:
        await synapse.close()

asyncio.run(main())
```

The Neuron is a plain async function. No base class, no inheritance, nothing to import into it.

## Architecture

<div align="center">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/diagrams/primitives-dark.svg" />
  <img src="assets/diagrams/primitives-light.svg" alt="Four participants on one synapse bus: a Receptor drawn as a cup above the bus, and a Neuron drawn as a circle, an Engram drawn as a diamond and an Effector drawn as a triangle below it." width="820" />
</picture>
</div>

Four kinds of participant hang off one Synapse, and the division of labour is the whole design: **Neurons think, Engrams remember, Effectors act, Receptors listen.** Each keeps one silhouette across the suite - you place that shape in Genesis and watch the same shape light up in Prism.

| Abstraction | Plays the role of | What it does |
| --- | --- | --- |
| **Signal** | The message | The typed envelope everything crosses the bus in - `TASK`, `AGENT_OUTPUT`, `TOOL_CALL`, `RECALL`, `FINAL`, … Two components that emit valid Signals can always talk. |
| **Neuron** | The unit of work | A pure async function `(input, context) -> output`. The factory wraps OpenAI, Anthropic, HuggingFace, Groq, Ollama or an MCP server behind that same signature. |
| **Axon** | Agent-side identity | Owns a Neuron's id and capabilities and wraps its output into protocol-valid Signals. An Axon never touches the Synapse - that boundary is enforced in code. |
| **Dendrite** | Synapse-side connector | The only component that touches the Synapse. Hosts Axons, emits `REGISTER` / `HEARTBEAT` / `DEREGISTER`, routes inbound TASKs, and exposes every dispatch primitive. Any Dendrite can orchestrate; there is no Cortex class. |
| **Engram** | Memory | Shared state as a protocol citizen rather than a library you import. `RECALL` and `IMPRINT` are Signals, so a memory read shows up in a trace. |
| **Effector** | Tools and side effects | Services `TOOL_CALL`, replies `TOOL_RESULT`. An MCP server is an Effector, not a Neuron. Tool errors ride the reply, so a failing tool never kills the TASK. |
| **Receptor** | The edge | A CLI, HTTP or chat surface that funnels an outside request into the same dispatch trio everything else uses. It adds no wire types, so nothing downstream knows the difference. |
| **Synapse** | The bus | Transports Signals between Dendrites - in-memory, a local TCP broker, NATS or Kafka. |

### The envelope

<div align="center">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/diagrams/envelope-dark.svg" />
  <img src="assets/diagrams/envelope-light.svg" alt="The Signal envelope as a card listing its fields - v, id, trace_id, parent_id, type, directed, payload and meta - with the three lineage fields highlighted and pointing to a small causal tree of Signals." width="820" />
</picture>
</div>

Every Signal carries its own id, the trace it belongs to, and the id of whatever caused it. That is not instrumentation you add - it is the envelope, filled in whether or not anyone intends to look. Which is why "why did this happen" is walking up a tree rather than grepping a log, and why you would have to work to *not* have a trace. See [`design/ENVELOPE_SPEC.md`](design/ENVELOPE_SPEC.md) for the full wire format, or run `cosmo schema` for the machine-readable version.

### Human-in-the-loop is a Signal type

A Neuron can pause and *ask* instead of returning. A `__clarification__` or `__permission__` marker becomes a `CLARIFICATION` / `PERMISSION` Signal that another Dendrite - or a person, through `cosmo answer` - resolves. Paired with an Engram, an answered question or a granted permission is recalled rather than re-asked. No custom plumbing, no separate approval service.

### Scale is a URL change

The transport is pluggable because Cosmonapse is a protocol, not a runtime. The same component code runs across every one of them.

| Transport | Best for | Connect |
| --- | --- | --- |
| In-memory | Unit tests and single-process apps | `connect_synapse("memory://")` |
| TCP broker | Local multi-process development | `connect_synapse("cosmo://127.0.0.1:7070")` |
| NATS | Production, low-latency fan-out | `pip install "cosmonapse[nats]"` |
| Kafka | Production, durable and replayable | `pip install "cosmonapse[kafka]"` |

Other extras: `[postgres]` for a Postgres-backed Engram, `[receptor]` for the FastAPI-served HTTP and chat Receptors. A `CliReceptor` needs nothing beyond the core install.

## Observability

<div align="center">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/diagrams/read-only-tap-dark.svg" />
  <img src="assets/diagrams/read-only-tap-light.svg" alt="A synapse bus with three participants below it connected by two-way arrows, and a read-only tap above it connected by a single dashed arrow pointing away from the bus." width="820" />
</picture>
</div>

An ordinary consumer joins a queue group, so exactly one of them gets each message. A **Doppler** joins none: it competes for nothing, sees every Signal, and can never take work away from a participant. `cosmo prism` is that seat rendered in a browser; `cosmo prism --tail` is the same stream on stdout. Neither is privileged - anything willing to speak the envelope can take the same seat.

## The `cosmo` CLI

| Command | Does |
| --- | --- |
| `cosmo init` | Scaffold a project |
| `cosmo genesis` | Open the designer |
| `cosmo prism` | Open the observability UI (`--tail` for stdout) |
| `cosmo synapse` | Start or inspect a local broker |
| `cosmo dispatch` | Fire a TASK from the terminal |
| `cosmo registry` | List who has registered |
| `cosmo answer` | Resolve a clarification or permission by hand |
| `cosmo schema` | Print the envelope JSON Schema |
| `cosmo validate` | Check envelope conformance |
| `cosmo completion` | Install shell completion |

Python is the single first-party SDK. Nothing about the protocol is Python, though: the [envelope spec](design/ENVELOPE_SPEC.md) plus `cosmo schema` is everything an implementation in another language builds against, and anything that emits and accepts valid Signals is a participant on the bus. See [DECISIONS #19](design/DECISIONS.md) for why there is one reference implementation rather than two.

## Repository Map

| Path | Purpose |
| --- | --- |
| `packages/python-sdk/` | The `cosmonapse` SDK and the bundled `cosmo` CLI |
| `packages/genesis-ui/` | Genesis, the designer |
| `packages/prism-ui/` | Prism, the observability UI |
| `design/ENVELOPE_SPEC.md` | Signal envelope / wire-format spec |
| `design/SDK_DESIGN.md` | Design rationale |
| `design/ENGRAM_DESIGN.md` | Engram (shared memory) design |
| `design/RECEPTOR_DESIGN.md` | Receptor (interface layer) design |
| `design/ROADMAP.md` | Roadmap and milestones |
| `design/DECISIONS.md` | Architecture decision log |

Runnable end-to-end examples - routing, bidding, retries, RAG, MCP tools, memory, full agents - live in their own repository: [Cosmonapse/cosmonapse-examples](https://github.com/Cosmonapse/cosmonapse-examples).

## Documentation

- [Python SDK README](packages/python-sdk/README.md) - install, quick start, API, CLI
- [ENVELOPE_SPEC.md](design/ENVELOPE_SPEC.md) - the Signal wire format
- [SDK_DESIGN.md](design/SDK_DESIGN.md) - design rationale
- [ENGRAM_DESIGN.md](design/ENGRAM_DESIGN.md) - shared memory design
- [RECEPTOR_DESIGN.md](design/RECEPTOR_DESIGN.md) - interface layer design
- [CONTRIBUTING.md](CONTRIBUTING.md) - how to set up and contribute
- [CHANGELOG.md](CHANGELOG.md) - release notes

The diagrams above are generated from the same components that draw them on [cosmonapse.com](https://cosmonapse.com), so a figure here cannot drift from the one on the site.

## License

[Apache 2.0](LICENSE) © 2026 Aqib Khan
