"""
Neuron / Axon / Dendrite combo for an OpenAI agent.

Roles in play:
  * Neuron  -- the OpenAI agent (black box; zero protocol knowledge).
  * Axon    -- the adapter wrapping that Neuron; recognises the agent's output
               as AGENT_OUTPUT / CLARIFICATION / PERMISSION / ERROR.
  * Dendrite-- the only thing on the Synapse. One worker hosts the Axon; one
               orchestrator dispatches the TASK and waits for the reply.

Everything runs in a single process over MemorySynapse -- no infra needed.

Run:
    export OPENAI_API_KEY=sk-...
    pip install httpx
    python examples/openai_agent/main.py
"""

import asyncio

from cosmonapse import Axon, Dendrite, MemorySynapse
from cosmonapse.envelope import SignalType

# A system prompt that teaches the agent the one convention the Axon's
# recogniser looks for: to ask a question instead of answering, reply with a
# single JSON object carrying a "cosmo" key. Plain prose is treated as output.
SYSTEM = """You are a helpful writing assistant.
If you can answer, reply with your answer as plain text.
If you need clarification before answering, reply with EXACTLY one JSON object:
  {"cosmo": "clarification", "question": "<your question>"}
Do not wrap it in prose."""


async def main() -> None:
    # 1) Neuron + Axon in one call. `Axon.openai(...)` builds an Axon already
    #    paired with Neuron(source="openai") and the LLM recogniser.
    #    api_key is read from OPENAI_API_KEY if not passed explicitly.
    writer = Axon.openai(
        "writer",
        model="gpt-4o-mini",
        system=SYSTEM,
        capabilities=["writing", "text"],
        version="0.1.0",
    )

    # 2) The Synapse. The caller owns it; the Dendrites just borrow it.
    synapse = MemorySynapse()
    await synapse.connect()

    # 3) Two Dendrites on the same Synapse.
    #    - worker hosts the Axon (role="worker": hosts, never dispatches).
    #    - orch dispatches the TASK (role="orchestrator", the default).
    worker = Dendrite(synapse=synapse, namespace="demo", role="worker")
    worker.attach_axon(writer)

    orch = Dendrite(synapse=synapse, namespace="demo", dendrite_id="orch")

    try:
        async with worker, orch:
            # 4) Dispatch a TASK addressed to the "writer" neuron and block
            #    until its terminal Signal comes back over the Synapse.
            reply = await orch.dispatch_and_wait(
                neuron="writer",
                input={"prompt": "Write a one-line tagline for a coffee shop."},
                timeout_s=60,
            )

            # 5) The Axon classified the agent's output for us.
            if reply.type is SignalType.AGENT_OUTPUT:
                print("ANSWER:", reply.payload["output"]["response"])
            elif reply.type is SignalType.CLARIFICATION:
                print("AGENT ASKS:", reply.payload["question"])
                # ...here you'd answer and re-dispatch (respond_to_clarification)
            elif reply.type is SignalType.ERROR:
                print("ERROR:", reply.payload.get("message"))
    finally:
        await synapse.close()


if __name__ == "__main__":
    asyncio.run(main())
