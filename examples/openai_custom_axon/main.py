"""
Stock OpenAI Neuron + a CUSTOM Axon.

The Neuron is the built-in source, unchanged:

    Neuron(source="openai", model="gpt-4o-mini")

It returns the stock LLM shape: {"response": "<text>", "meta": <raw>}.

What's custom is the Axon. Instead of the default factory recogniser (which
looks for a {"cosmo": ...} JSON block), we attach our OWN recognition scheme
over that same {"response": text} output -- here, simple line prefixes:

    ASK:  <question>   -> CLARIFICATION
    NEED: <action>     -> PERMISSION
    <anything else>    -> AGENT_OUTPUT (reshaped our way)

Same Neuron, different Axon handling. The Neuron never changes; only how the
Axon reads it does.

Run:
    export OPENAI_API_KEY=sk-...
    pip install httpx
    python examples/openai_custom_axon/main.py
"""

import asyncio

from cosmonapse import Axon, Dendrite, MemorySynapse, Neuron
from cosmonapse.envelope import SignalType

SYSTEM = """You are a writing assistant.
- To answer, reply with the answer as plain text.
- If you must ask something first, reply with a single line starting 'ASK:'.
- If you need approval before acting, reply with a single line starting 'NEED:'."""


# ---------------------------------------------------------------------------
# The custom Axon handling: a recogniser over the STOCK OpenAI output
# ({"response": text, "meta": ...}). This is the only custom piece.
# ---------------------------------------------------------------------------
def openai_prefix_recogniser(raw):
    text = (raw.get("response") or "").strip() if isinstance(raw, dict) else str(raw)
    if text.startswith("ASK:"):
        return {"__clarification__": True, "question": text[len("ASK:"):].strip()}
    if text.startswith("NEED:"):
        return {"__permission__": True, "action": text[len("NEED:"):].strip()}
    # Plain answer -- reshape into our own output schema.
    meta = raw.get("meta") if isinstance(raw, dict) else None
    return {"answer": text, "model": (meta or {}).get("model")}


async def main():
    # Stock neuron source; custom Axon via output_parser.
    agent = Axon(
        neuron_id="writer",
        neuron_fn=Neuron(source="openai", model="gpt-4o-mini", system=SYSTEM),
        output_parser=openai_prefix_recogniser,   # <-- custom handling
        capabilities=["writing", "text"],
        version="0.1.0",
    )

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
                print("ANSWER:", out["answer"], "  (model:", out["model"], ")")
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
