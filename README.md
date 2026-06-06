# Cosmonapse

Event-driven **Agent-to-Agent (A2A) protocol** with a Python SDK, a TypeScript
SDK, and a developer CLI (`cosmo`).

Cosmonapse models multi-agent systems on a neural metaphor: agents are pure
functions (**Neurons**), wrapped by **Axons** that turn their output into
protocol-valid **Signals**, attached to **Dendrites** that exchange those
signals over a **Synapse** (an in-memory bus, a local dev TCP broker, NATS, or
Kafka). There is no central orchestrator class  -  any Dendrite can dispatch work
and react to results.

A Neuron can also pause and *ask* instead of returning a result: a
`__clarification__` or `__permission__` marker becomes a `CLARIFICATION` /
`PERMISSION` signal that another Dendrite (a central Cortex or any peer) answers
 -  pairing naturally with an **Engram** (shared memory) so grants and answers
are recalled instead of re-asked. See `design/ENVELOPE_SPEC.md` §7.5.

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

## Repository layout

```
cosmonapse-core/
├── packages/
│   ├── python-sdk/      The cosmonapse SDK + bundled `cosmo` CLI (see its README)
│   └── ts-sdk/          TypeScript SDK
├── examples/            Runnable end-to-end examples
└── design/              Design docs & specs
    ├── SDK_DESIGN.md        Design rationale
    ├── ENVELOPE_SPEC.md     Signal envelope / wire-format spec
    ├── ENGRAM_DESIGN.md     Engram (shared memory) design
    ├── ROADMAP.md           Roadmap & milestones
    └── DECISIONS.md         Architecture decision log
```

## Install (Python)

```bash
pip install -e cosmonapse-core/packages/python-sdk
```

This installs both `import cosmonapse` and the `cosmo` command. Optional extras:
`[nats]`, `[kafka]`, `[postgres]`, and `[flask]`. See
[`packages/python-sdk/README.md`](packages/python-sdk/README.md) for full SDK
and CLI documentation.

## Documentation

- [Python SDK README](packages/python-sdk/README.md)  -  install, quick start, API, CLI
- [SDK_DESIGN.md](design/SDK_DESIGN.md)  -  design rationale
- [ENVELOPE_SPEC.md](design/ENVELOPE_SPEC.md)  -  the Signal wire format
- [CONTRIBUTING.md](CONTRIBUTING.md)  -  how to set up and contribute
- [CHANGELOG.md](CHANGELOG.md)  -  release notes

## License

[MIT](LICENSE) © 2026 Aqib Khan
