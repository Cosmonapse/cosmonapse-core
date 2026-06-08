"""
Custom-built OpenAI Neuron + a modified Axon.

Instead of the batteries-included `Axon.openai(...)` factory, this builds:

  * a hand-written Neuron  -- your own OpenAI call, returning the model's
    NATIVE structured shape ({"kind": ...}); it knows nothing about the
    protocol, and it never produces markers.
  * a modified Axon        -- a plain Axon given a custom `output_parser`
    (the extension point). The parser is the recognition half: it maps the
    neuron's {"kind": ...} vocabulary onto the Axon's markers, so the Axon
    emits AGENT_OUTPUT / CLARIFICATION / PERMISSION / ERROR.

This is the split we want: Neuron decides, Axon translates. Recognition lives
in the Axon, not the Neuron -- so you can swap the Neuron without touching the
protocol, and tune recognition without touching the Neuron.

Run:
    export OPENAI_API_KEY=sk-...
    pip install httpx
    python examples/openai_custom/main.py
"""

import asyncio
import json
import os

import httpx

from cosmonapse import Axon, Dendrite, MemorySynapse
from cosmonapse.envelope import SignalType

OPENAI_URL = "https://api.openai.com/v1/chat/completions"

# We instruct the model to speak its OWN vocabulary. The Axon recogniser below
# maps it -- the model never needs to know cosmonapse marker names.
SYSTEM = """You are a careful assistant. Reply with exactly ONE JSON object:
  to answer:            {"kind": "answer", "text": "<answer>"}
  to ask a question:    {"kind": "ask", "question": "<question>"}
  to request approval:  {"kind": "approve", "action": "<action>", "reason": "<why>"}"""


# ---------------------------------------------------------------------------
# 1) The custom Neuron -- a pure async function. Returns the model's native
#    {"kind": ...} dict. Optionally uses the injected `recall` helper if the
#    Axon was wired with an Engram (inline capability), but degrades gracefully.
# ---------------------------------------------------------------------------
async def openai_agent(input, context, *, recall=None):
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set")

    messages = [{"role": "system", "content": SYSTEM}]

    # Inline capability: hydrate from memory if an Engram is bound to this Axon.
    if recall is not None:
        try:
            prior = await recall("memory", query={"text": input["prompt"]})
            if len(prior):
                messages.append({"role": "system",
                                 "content": f"Prior note: {prior.hits[0].entry}"})
        except Exception:
            pass  # no memory wired / miss -> proceed without it

    messages.append({"role": "user", "content": input["prompt"]})

    body = {
        "model": input.get("model", "gpt-4o-mini"),
        "messages": messages,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(OPENAI_URL, json=body, headers=headers)
        r.raise_for_status()
        data = r.json()

    text = data["choices"][0]["message"]["content"]
    return json.loads(text)  # native {"kind": ...} -- recognition is the Axon's job


# ---------------------------------------------------------------------------
# 2) The Axon modification -- a custom output_parser. It translates the
#    neuron's {"kind": ...} vocabulary into the marker dict Axon.handle_task
#    understands. Raising or an __error__ marker yields an ERROR Signal.
# ---------------------------------------------------------------------------
def recognize_agent(raw):
    if not isinstance(raw, dict):
        return {"value": raw}
    kind = raw.get("kind")
    if kind == "ask":
        return {"__clarification__": True, "question": raw.get("question", "")}
    if kind == "approve":
        return {"__permission__": True,
                "action": raw.get("action", ""),
                "reason": raw.get("reason")}
    if kind == "answer":
        return {"answer": raw.get("text", "")}
    return {"__error__": True, "code": "BAD_OUTPUT",
            "message": f"unexpected kind: {kind!r}"}


async def main():
    # 3) Plain Axon + the custom neuron + the custom recogniser.
    agent = Axon(
        neuron_id="assistant",
        neuron_fn=openai_agent,
        output_parser=recognize_agent,   # <-- the Axon modification
        capabilities=["chat"],
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
                neuron="assistant",
                input={"prompt": "What is the capital of France?"},
                timeout_s=60,
            )
            if reply.type is SignalType.AGENT_OUTPUT:
                print("ANSWER:", reply.payload["output"]["answer"])
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
