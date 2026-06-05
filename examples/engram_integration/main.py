"""
examples/engram_integration/main.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Integrating an Engram  -  shared memory bound to a Neuron.

Three things change versus `building_a_neuron`:

  1. An Engram backend (`InMemoryEngram`) is attached to a host Dendrite.
     This is what answers RECALL / IMPRINT signals on the bus.

  2. The worker Axon declares an `EngramBinding`: a local name (here
     `"ctx"`) mapped to the engram_id `"ctx"` on the wire. The Axon
     stores these bindings; the Dendrite injects `recall` and `imprint`
     helpers into the Neuron at call time.

  3. The Neuron signature gains two keyword-only parameters:
     `recall` and `imprint`. Calling them emits RECALL / IMPRINT Signals
     under the current trace_id and waits for the matching reply
     (RECALLED / IMPRINTED). The Neuron stays pure  -  it never imports
     the protocol or touches the Synapse.

The example calls the Neuron twice with the same input. The first call
imprints an answer; the second call recalls it before computing,
proving the write landed.

Run:

    pip install cosmonapse
    python examples/engram_integration/main.py
"""
from __future__ import annotations

import asyncio

from cosmonapse import (
    Axon,
    Dendrite,
    EngramBinding,
    InMemoryEngram,
    MemorySynapse,
)


# ---------------------------------------------------------------------------
# The Neuron.
#
# `recall` and `imprint` are keyword-only callables injected by the Axon
# because the Axon was constructed with `engrams=[EngramBinding(name="ctx",
# engram_id="ctx")]`. They take the local binding name (`"ctx"`) as the
# first positional argument so the Neuron stays decoupled from deployment
# identifiers  -  operations change the engram_id without touching the
# Neuron.
# ---------------------------------------------------------------------------
async def researcher(input: dict, context: list, *, recall, imprint) -> dict:
    question = input["question"]

    # 1. Look in shared memory for a prior answer to this exact question.
    prior = await recall("ctx", query={"text": question})
    if prior.hits:
        # Use the cached answer; the Engram already knows.
        cached = prior.hits[0].content["answer"]
        return {"answer": cached, "source": "cache"}

    # 2. Compute a "fresh" answer (just a stub for the demo).
    answer = f"Answer to {question!r}: 42"

    # 3. Write it back so the next call hits the cache.
    #    merge_key dedupes by question text so repeated imprints upsert
    #    a single entry per question.
    await imprint(
        "ctx",
        op="upsert",
        entry={"question": question, "answer": answer, "tags": ["qa"]},
        merge_key=f"q:{question}",
        await_ack=True,
        deadline_ms=500,
    )
    return {"answer": answer, "source": "computed"}


async def main() -> None:
    synapse = MemorySynapse()
    await synapse.connect()

    try:
        # -------------------------------------------------------------------
        # Engram host  -  a worker Dendrite that owns the memory backend.
        # `engram_id="ctx"` is the address other peers use to route
        # RECALL / IMPRINT to this specific backend.
        # -------------------------------------------------------------------
        host = Dendrite(
            synapse=synapse,
            namespace="demo",
            dendrite_id="engram-host",
            role="worker",
        )
        host.attach_engram(
            InMemoryEngram(engram_id="ctx", engram_kind="context")
        )

        # -------------------------------------------------------------------
        # Worker  -  hosts the Neuron and declares the binding.
        # -------------------------------------------------------------------
        worker = Dendrite(
            synapse=synapse,
            namespace="demo",
            dendrite_id="worker",
            role="worker",
        )
        worker.attach_axon(
            Axon(
                neuron_id="researcher",
                neuron_fn=researcher,
                capabilities=["research"],
                engrams=[EngramBinding(name="ctx", engram_id="ctx")],
            )
        )

        # -------------------------------------------------------------------
        # Orchestrator  -  drives two calls back-to-back.
        # -------------------------------------------------------------------
        orchestrator = Dendrite(synapse=synapse, namespace="demo")

        async with host, worker, orchestrator:
            for label in ("first call ", "second call"):
                reply = await orchestrator.dispatch_and_wait(
                    neuron="researcher",
                    input={"question": "what is the meaning of life?"},
                    timeout_s=5.0,
                )
                out = reply.payload["output"]
                print(f"[{label}] {out['source']:>8s}  →  {out['answer']}")
    finally:
        await synapse.close()


if __name__ == "__main__":
    asyncio.run(main())
