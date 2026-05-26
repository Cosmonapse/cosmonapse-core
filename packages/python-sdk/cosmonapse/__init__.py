"""
Cosmonapse Python SDK
~~~~~~~~~~~~~~~~~~~~~
Event-driven Agent-to-Agent protocol primitives.

Layers
------
  Neuron        Factory that wraps *anything that interacts with the real
                world* behind the NeuronFn signature — an LLM/agent (Ollama,
                HuggingFace TGI), an API (a Flask app or any WSGI callable),
                or an MCP server (any stdio MCP server). Also the conceptual
                name for any pure async function used as an agent — zero
                protocol knowledge.
  Axon          Agent-side tool. Validates output into a Signal.
  Dendrite      Synapse-side participant. Synapse required; everything
                else opt-in (attach Axons to enable TASK routing,
                register handlers to enable inbound subscriptions,
                pass a registry_store to enable persistence).
  Synapse       Synapse. Built and closed by the caller via
                connect_synapse(url).

Cortex
------
`Cortex` is now a back-compat alias for `Dendrite`. There is no
separate orchestrator class — every Dendrite has dispatch_task /
emit_final / emit_error / on_agent_output / on_clarification /
on_error / etc. Use Dendrite directly in new code.

Quick start
-----------
    import asyncio
    from cosmonapse import (
        Axon, Dendrite, MemoryRegistryStore,
        connect_synapse,
    )

    async def main():
        synapse = await connect_synapse("cosmo://127.0.0.1:7070")
        try:
            async def my_neuron(input, context):
                return {"answer": input["q"]}

            # Worker Dendrite: hosts an Axon
            worker = Dendrite(synapse=synapse, namespace="demo")
            worker.attach_axon(Axon(neuron_id="answerer", neuron_fn=my_neuron))

            # Orchestrator Dendrite: drives a workflow
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
"""

from cosmonapse.axon import Axon, NeuronFn, ContextFetcher
from cosmonapse.neuron import Neuron, STANDARD_MCP_SERVERS
from cosmonapse.dendrite import Dendrite, DendriteProtocolError, Cortex, CortexProtocolError
from cosmonapse.storage import (
    NeuronRecord,
    RegistryStore,
    MemoryRegistryStore,
    SqliteRegistryStore,
    PostgresRegistryStore,
)
from cosmonapse.envelope import (
    Signal,
    SignalType,
    AXON_TYPES,
    SYNAPSE_TYPES,
    new_event_id,
    new_trace_id,
    task_signal,
    agent_output_signal,
    clarification_signal,
    final_signal,
    error_signal,
    register_signal,
    deregister_signal,
    heartbeat_signal,
    memory_append_signal,
    task_offer_signal,
    bid_signal,
    critique_signal,
)
from cosmonapse.synapse import (
    Synapse,
    MemorySynapse,
    DevSynapse,
    DevSynapseServer,
    NatsSynapse,
    KafkaSynapse,
)
from cosmonapse._url import synapse_from_url, connect_synapse

__version__ = "0.0.1"

__all__ = [
    "Signal",
    "SignalType",
    "AXON_TYPES",
    "SYNAPSE_TYPES",
    "new_event_id",
    "new_trace_id",
    "task_signal",
    "agent_output_signal",
    "clarification_signal",
    "final_signal",
    "error_signal",
    "register_signal",
    "deregister_signal",
    "heartbeat_signal",
    "memory_append_signal",
    "task_offer_signal",
    "bid_signal",
    "critique_signal",
    "Neuron",
    "STANDARD_MCP_SERVERS",
    "Axon",
    "NeuronFn",
    "ContextFetcher",
    "Dendrite",
    "DendriteProtocolError",
    "Cortex",
    "CortexProtocolError",
    "NeuronRecord",
    "Synapse",
    "MemorySynapse",
    "DevSynapse",
    "DevSynapseServer",
    "NatsSynapse",
    "KafkaSynapse",
    "RegistryStore",
    "MemoryRegistryStore",
    "SqliteRegistryStore",
    "PostgresRegistryStore",
    "synapse_from_url",
    "connect_synapse",
]
