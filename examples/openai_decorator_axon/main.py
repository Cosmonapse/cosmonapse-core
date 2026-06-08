"""
Stock OpenAI Neuron + a decorator-built Axon.

Same idea as the custom-Axon example, but the recognition is declared with
decorators -- the same model the Dendrite uses for its @on_* handlers. The
Neuron is the built-in source; the Axon's behaviour is assembled by decorating
detector functions onto it.

    @axon.detects_clarification -> return {"question": ...} or None
    @axon.detects_permission    -> return {"action": ...} or None
    @axon.detects_error         -> return {"code": ..., "message": ...} or None
    @axon.detects_output        -> return the AGENT_OUTPUT payload, or None

The ``detects_*`` name marks the asking side (recognising the Neuron's output),
distinct from the Dendrite's ``on_*`` handlers (consuming inbound Signals).
Detectors are tried in precedence error -> clarification -> permission ->
output. They may be sync or async. The Axon also supports the lifecycle
decorators it mixes in from LifecycleHooks (@axon.on_connect, etc.).

Run:
    export OPENAI_API_KEY=sk-...
    pip install httpx
    python examples/openai_decorator_axon/main.py
"""

import asyncio

from cosmonapse import Axon, Dendrite, MemorySynapse, Neuron
from cosmonapse.envelope import SignalType

SYSTEM = """You are a writing assistant.
- To answer, reply with the answer as plain text.
- To ask first, reply with one line starting 'ASK:'.
- To request approval, reply with one line starting 'NEED:'."""


def build_axon() -> Axon:
    # Stock neuron source; behaviour assembled via decorators.
    axon = Axon(
        neuron_id="writer",
        neuron_fn=Neuron(source="openai", model="gpt-4o-mini", system=SYSTEM),
        capabilities=["writing", "text"],
        version="0.1.0",
    )

    @axon.detects_clarification
    def detect_ask(raw):
        t = (raw.get("response") or "").strip()
        return {"question": t[len("ASK:"):].strip()} if t.startswith("ASK:") else None

    @axon.detects_permission
    def detect_need(raw):
        t = (raw.get("response") or "").strip()
        return {"action": t[len("NEED:"):].strip()} if t.startswith("NEED:") else None

    @axon.detects_output
    def shape_answer(raw):
        meta = raw.get("meta") or {}
        return {"answer": (raw.get("response") or "").strip(), "model": meta.get("model")}

    # Lifecycle decorator (already provided by the Axon) -- runs once on attach.
    @axon.on_connect
    async def warm(a):
        print(f"axon {a.neuron_id} ready")

    return axon


async def main():
    agent = build_axon()

    synapse = MemorySynapse()
    await synapse.connect()

    worker = Dendrite(synapse=synapse, namespace="demo", role="worker")
    worker.attach_axon(agent)
    orch = Dendrite(synapse=synapse, namespace="demo", dendrite_id="orch")

    try:
        async with worker, orch:
            reply = await orch.dispatch_and_wait(
                neuron="writer",
                input={"prompt": "Write a one-line tagline for a coffee shop."},
                timeout_s=60,
            )
            if reply.type is SignalType.AGENT_OUTPUT:
                out = reply.payload["output"]
                print("ANSWER:", out["answer"], " (model:", out["model"], ")")
            elif reply.type is SignalType.CLARIFICATION:
                print("AGENT ASKS:", reply.payload["question"])
            elif reply.type is SignalType.PERMISSION:
                print("WANTS APPROVAL:", reply.payload["action"])
            elif reply.type is SignalType.ERROR:
                print("ERROR:", reply.payload.get("message"))
    finally:
        await synapse.close()


if __name__ == "__main__":
    asyncio.run(main())
