"""
Envelope conformance tests.

These tests define correct envelope behaviour.
Any third-party codec must pass these to be considered conformant.
"""


# We test the logic directly without Pydantic since the sandbox has no deps.
# When running with `pip install -e ".[dev]"`, Pydantic validates everything.
# These tests check the contracts that Pydantic enforces.


def test_event_id_prefix():
    """Signal IDs must start with 'evt_'."""
    from cosmonapse.envelope import new_event_id
    eid = new_event_id()
    assert eid.startswith("evt_"), f"Expected 'evt_' prefix, got: {eid!r}"
    assert len(eid) == 4 + 26  # "evt_" + 26-char ULID


def test_trace_id_prefix():
    """Trace IDs must start with 'trc_'."""
    from cosmonapse.envelope import new_trace_id
    tid = new_trace_id()
    assert tid.startswith("trc_"), f"Expected 'trc_' prefix, got: {tid!r}"
    assert len(tid) == 4 + 26


def test_event_ids_are_unique():
    """Each new_event_id() call returns a distinct value."""
    from cosmonapse.envelope import new_event_id
    ids = {new_event_id() for _ in range(1000)}
    assert len(ids) == 1000


def test_signal_round_trips_json():
    """A Signal encodes to JSON and decodes back to an identical object."""
    from cosmonapse import Signal, SignalType, Directed, new_trace_id

    original = Signal(
        type=SignalType.TASK,
        trace_id=new_trace_id(),
        payload={"input": {"text": "hello"}},
        directed=Directed(id="test-neuron"),
    )
    encoded = original.encode()
    decoded = Signal.decode(encoded)

    assert decoded.id == original.id
    assert decoded.trace_id == original.trace_id
    assert decoded.type == original.type
    assert decoded.payload == original.payload
    assert decoded.directed == original.directed
    assert decoded.directed.id == "test-neuron"


def test_task_signal_constructor():
    """task_signal() produces a well-formed TASK envelope."""
    from cosmonapse import task_signal, SignalType

    sig = task_signal(input={"task": "build a website"})
    assert sig.type == SignalType.TASK
    assert sig.payload["input"] == {"task": "build a website"}
    assert sig.id.startswith("evt_")
    assert sig.trace_id.startswith("trc_")


def test_agent_output_signal_constructor():
    """agent_output_signal() wraps output in a neutral AGENT_OUTPUT envelope."""
    from cosmonapse import agent_output_signal, SignalType, Directed, new_trace_id, new_event_id

    trace = new_trace_id()
    parent = new_event_id()
    sig = agent_output_signal(
        trace_id=trace,
        parent_id=parent,
        directed=Directed(id="my-neuron"),
        output={"result": 42},
    )
    assert sig.type == SignalType.AGENT_OUTPUT
    assert sig.trace_id == trace
    assert sig.parent_id == parent
    assert sig.directed.id == "my-neuron"
    assert sig.payload["output"] == {"result": 42}


def test_clarification_signal():
    """clarification_signal() produces a CLARIFICATION with question field."""
    from cosmonapse import clarification_signal, SignalType, Directed, new_trace_id, new_event_id

    sig = clarification_signal(
        trace_id=new_trace_id(),
        parent_id=new_event_id(),
        directed=Directed(id="agent-1"),
        question="Which database?",
        context={"options": ["postgres", "mysql"]},
    )
    assert sig.type == SignalType.CLARIFICATION
    assert sig.payload["question"] == "Which database?"
    assert sig.payload["context"]["options"] == ["postgres", "mysql"]


def test_signal_reply():
    """Signal.reply() produces a child signal that inherits trace_id and sets parent_id."""
    from cosmonapse import task_signal, SignalType

    parent = task_signal(input={"x": 1})
    reply = parent.reply(type=SignalType.AGENT_OUTPUT, payload={"output": {}})

    assert reply.trace_id == parent.trace_id
    assert reply.parent_id == parent.id
    assert reply.type == SignalType.AGENT_OUTPUT


def test_register_signal_has_own_trace():
    """Management signals (REGISTER) get their own trace_id, not a workflow trace."""
    from cosmonapse import register_signal, SignalType, Directed, new_trace_id

    workflow_trace = new_trace_id()
    sig = register_signal(directed=Directed(id="my-agent"), capabilities=["nlp"])

    assert sig.type == SignalType.REGISTER
    assert sig.trace_id != workflow_trace  # its own independent trace
    assert sig.payload["capabilities"] == ["nlp"]


def test_axon_type_set():
    """AXON_TYPES contains exactly the types the Axon is allowed to produce."""
    from cosmonapse import AXON_TYPES, SignalType

    expected = {
        SignalType.AGENT_OUTPUT,
        SignalType.CLARIFICATION,
        SignalType.PERMISSION,
        SignalType.ERROR,
        SignalType.REGISTER,
        SignalType.DEREGISTER,
        SignalType.HEARTBEAT,
    }
    assert AXON_TYPES == expected


def test_synapse_types_dont_overlap_with_axon_exclusive():
    """FINAL, TASK, TASK_OFFER etc. are Cortex-only and not in AXON_TYPES."""
    from cosmonapse import AXON_TYPES, SYNAPSE_TYPES, SignalType

    nucleus_exclusive = {
        SignalType.TASK,
        SignalType.FINAL,
        SignalType.TASK_OFFER,
        SignalType.BID,
        SignalType.MEMORY_APPEND,
    }
    for t in nucleus_exclusive:
        assert t not in AXON_TYPES, f"{t} should not be in AXON_TYPES"
        assert t in SYNAPSE_TYPES, f"{t} should be in SYNAPSE_TYPES"


def test_memory_synapse_publish_subscribe():
    """MemorySynapse delivers signals to all subscribers on a matching subject."""
    import asyncio
    from cosmonapse import MemorySynapse, task_signal, SignalType

    received = []

    async def run():
        synapse = MemorySynapse()
        await synapse.connect()

        async def handler(sig):
            received.append(sig)

        await synapse.subscribe("cosmonapse.test.>", handler)
        sig = task_signal(input={"x": 1})
        await synapse.publish("cosmonapse.test.TASK", sig)
        await asyncio.sleep(0.01)
        await synapse.close()

    asyncio.run(run())
    assert len(received) == 1
    assert received[0].type == SignalType.TASK


def test_memory_synapse_wildcard_star():
    """* wildcard matches exactly one subject token."""
    import asyncio
    from cosmonapse import MemorySynapse, task_signal

    received_subjects = []

    async def run():
        synapse = MemorySynapse()
        await synapse.connect()

        async def handler(sig):
            received_subjects.append(True)

        # Subscribe with * wildcard  -  matches cosmonapse.ns.ANYTHING
        await synapse.subscribe("cosmonapse.ns.*", handler)

        sig = task_signal(input={})
        await synapse.publish("cosmonapse.ns.TASK", sig)
        await synapse.publish("cosmonapse.ns.FINAL", sig)
        # Should NOT match (two extra tokens)
        await synapse.publish("cosmonapse.ns.extra.TASK", sig)
        await asyncio.sleep(0.01)
        await synapse.close()

    asyncio.run(run())
    assert len(received_subjects) == 2


def test_memory_synapse_queue_group_load_balancing():
    """With a queue_group, only one subscriber in the group receives each message."""
    import asyncio
    from cosmonapse import MemorySynapse, task_signal

    counts = [0, 0]

    async def run():
        synapse = MemorySynapse()
        await synapse.connect()

        async def handler_a(sig):
            counts[0] += 1

        async def handler_b(sig):
            counts[1] += 1

        await synapse.subscribe("cosmonapse.t.TASK", handler_a, queue_group="workers")
        await synapse.subscribe("cosmonapse.t.TASK", handler_b, queue_group="workers")

        for _ in range(10):
            sig = task_signal(input={})
            await synapse.publish("cosmonapse.t.TASK", sig)
        await asyncio.sleep(0.05)
        await synapse.close()

    asyncio.run(run())
    # Each message goes to exactly one worker  -  total == 10, neither is 0
    assert counts[0] + counts[1] == 10
    assert counts[0] > 0
    assert counts[1] > 0


def test_memory_synapse_doppler_receives_all():
    """A Doppler (no queue_group) receives every message even alongside queue-grouped subscribers."""
    import asyncio
    from cosmonapse import MemorySynapse, task_signal

    doppler_seen = []
    worker_seen = []

    async def run():
        synapse = MemorySynapse()
        await synapse.connect()

        async def doppler_handler(sig):
            doppler_seen.append(sig)

        async def worker_handler(sig):
            worker_seen.append(sig)

        # Doppler: no queue_group
        await synapse.subscribe("cosmonapse.d.>", doppler_handler, queue_group=None)
        # Worker: with queue_group
        await synapse.subscribe("cosmonapse.d.TASK", worker_handler, queue_group="workers")

        for _ in range(5):
            await synapse.publish("cosmonapse.d.TASK", task_signal(input={}))
        await asyncio.sleep(0.05)
        await synapse.close()

    asyncio.run(run())
    assert len(doppler_seen) == 5   # Doppler sees all 5
    assert len(worker_seen) == 5    # Worker sees all 5 too (only one in group)


def test_dendrite_emits_register_and_deregister_for_its_axons():
    """A Dendrite emits REGISTER on start and DEREGISTER on stop for each attached Axon."""
    import asyncio
    from cosmonapse import Axon, Dendrite, MemorySynapse, SignalType

    received = []

    async def run():
        synapse = MemorySynapse()
        await synapse.connect()

        await synapse.subscribe("cosmonapse.default.>", lambda s: received.append(s))

        async def my_neuron(input, ctx):
            return {"done": True}

        axon = Axon(neuron_id="test-agent", neuron_fn=my_neuron, capabilities=["test"])
        dendrite = Dendrite(synapse=synapse, namespace="default")
        dendrite.attach_axon(axon)

        async with dendrite:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.01)
        await synapse.close()

    asyncio.run(run())

    types_seen = [s.type for s in received]
    assert SignalType.REGISTER in types_seen
    assert SignalType.DEREGISTER in types_seen


def test_dendrite_routes_task_and_publishes_agent_output():
    """A TASK on the Synapse is routed to the matching Axon; the result is published as AGENT_OUTPUT."""
    import asyncio
    from cosmonapse import Axon, Dendrite, Directed, MemorySynapse, SignalType, task_signal

    outputs = []

    async def run():
        synapse = MemorySynapse()
        await synapse.connect()

        async def my_neuron(input, ctx):
            return {"answer": input.get("q", "?")}

        axon = Axon(neuron_id="answerer", neuron_fn=my_neuron)
        dendrite = Dendrite(synapse=synapse, namespace="t")
        dendrite.attach_axon(axon)

        await synapse.subscribe(
            "cosmonapse.t.AGENT_OUTPUT",
            lambda s: outputs.append(s),
        )

        async with dendrite:
            sig = task_signal(directed=Directed(id="answerer"), input={"q": "42"})
            await synapse.publish("cosmonapse.t.TASK", sig)
            await asyncio.sleep(0.05)

        await synapse.close()

    asyncio.run(run())
    assert len(outputs) == 1
    assert outputs[0].type == SignalType.AGENT_OUTPUT
    assert outputs[0].payload["output"]["answer"] == "42"


def test_axon_emits_clarification_when_neuron_signals_it():
    """When the Neuron returns {'__clarification__': True, ...} the Dendrite publishes CLARIFICATION."""
    import asyncio
    from cosmonapse import Axon, Dendrite, Directed, MemorySynapse, SignalType, task_signal

    clarifications = []

    async def run():
        synapse = MemorySynapse()
        await synapse.connect()

        async def needs_info_neuron(input, ctx):
            return {
                "__clarification__": True,
                "question": "Which language?",
                "context": {"options": ["python", "typescript"]},
            }

        axon = Axon(neuron_id="clarifier", neuron_fn=needs_info_neuron)
        dendrite = Dendrite(synapse=synapse, namespace="c")
        dendrite.attach_axon(axon)

        await synapse.subscribe(
            "cosmonapse.c.CLARIFICATION",
            lambda s: clarifications.append(s),
        )

        async with dendrite:
            await synapse.publish(
                "cosmonapse.c.TASK",
                task_signal(directed=Directed(id="clarifier"), input={"task": "build api"}),
            )
            await asyncio.sleep(0.05)

        await synapse.close()

    asyncio.run(run())
    assert len(clarifications) == 1
    assert clarifications[0].type == SignalType.CLARIFICATION
    assert clarifications[0].payload["question"] == "Which language?"


def test_axon_emits_error_on_neuron_exception():
    """If the Neuron raises, the Axon returns an ERROR Signal and the Dendrite publishes it."""
    import asyncio
    from cosmonapse import Axon, Dendrite, Directed, MemorySynapse, SignalType, task_signal

    errors = []

    async def run():
        synapse = MemorySynapse()
        await synapse.connect()

        async def broken_neuron(input, ctx):
            raise RuntimeError("something went wrong")

        axon = Axon(neuron_id="breaker", neuron_fn=broken_neuron)
        dendrite = Dendrite(synapse=synapse, namespace="e")
        dendrite.attach_axon(axon)

        await synapse.subscribe(
            "cosmonapse.e.ERROR",
            lambda s: errors.append(s),
        )

        async with dendrite:
            await synapse.publish(
                "cosmonapse.e.TASK",
                task_signal(directed=Directed(id="breaker"), input={}),
            )
            await asyncio.sleep(0.05)

        await synapse.close()

    asyncio.run(run())
    assert len(errors) == 1
    assert errors[0].type == SignalType.ERROR
    assert errors[0].payload["code"] == "NEURON_EXCEPTION"
    assert "something went wrong" in errors[0].payload["message"]
