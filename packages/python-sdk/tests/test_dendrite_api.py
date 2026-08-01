"""
Tests for the Dendrite public API surface and protocol guards.

These pin down the behaviour changed in the structure/API cleanup:
  * emit() refuses Axon-owned signal types; publish() is no longer public.
  * the short handler aliases are deprecated in favour of the _signal forms.
  * the cortex_id back-compat attribute is gone.
  * ContextFetcher is importable from the top-level package.
"""

import asyncio
import warnings

import pytest

from cosmonapse import (
    Dendrite,
    DendriteProtocolError,
    Directed,
    MemorySynapse,
    register_signal,
    heartbeat_signal,
    final_signal,
    new_trace_id,
    new_event_id,
)


def _run(coro):
    return asyncio.run(coro)


async def _make_dendrite():
    synapse = MemorySynapse()
    await synapse.connect()
    return synapse, Dendrite(synapse=synapse, namespace="t")


def test_context_fetcher_is_exported():
    # Regression: ContextFetcher used to be defined in axon.py but never exported.
    from cosmonapse import ContextFetcher  # noqa: F401
    import cosmonapse
    assert "ContextFetcher" in cosmonapse.__all__


def test_publish_is_private_not_public():
    # publish() was public and bypassed the protocol guard; it is now _publish().
    assert not hasattr(Dendrite, "publish"), "Dendrite.publish must not be public"
    assert hasattr(Dendrite, "_publish")


def test_no_cortex_id_attribute():
    async def run():
        synapse, dendrite = await _make_dendrite()
        try:
            assert not hasattr(dendrite, "cortex_id")
            assert dendrite.dendrite_id == "dendrite"
        finally:
            await synapse.close()
    _run(run())


def test_emit_rejects_axon_owned_types():
    async def run():
        synapse, dendrite = await _make_dendrite()
        try:
            # REGISTER and HEARTBEAT are Axon-owned: a Dendrite must not emit them
            # directly via the public emit() path.
            with pytest.raises(DendriteProtocolError):
                await dendrite.emit(register_signal(directed=Directed(id="x"), capabilities=[]))
            with pytest.raises(DendriteProtocolError):
                await dendrite.emit(heartbeat_signal(directed=Directed(id="x")))
        finally:
            await synapse.close()
    _run(run())


def test_emit_accepts_synapse_types():
    async def run():
        synapse, dendrite = await _make_dendrite()
        seen = []
        try:
            await synapse.subscribe("cosmonapse.t.FINAL", lambda s: seen.append(s))
            sig = final_signal(trace_id=new_trace_id(), parent_id=new_event_id(),
                               directed=Directed(id="d"), result={"ok": True})
            await dendrite.emit(sig)  # FINAL is a synapse-side type -> allowed
            await asyncio.sleep(0.01)
            assert len(seen) == 1
        finally:
            await synapse.close()
    _run(run())


@pytest.mark.parametrize(
    "alias,canonical",
    [
        ("on_error", "on_error_signal"),
        ("on_register", "on_register_signal"),
        ("on_deregister", "on_deregister_signal"),
        ("on_heartbeat", "on_heartbeat_signal"),
    ],
)
def test_short_handler_aliases_are_deprecated(alias, canonical):
    async def run():
        synapse, dendrite = await _make_dendrite()
        try:
            async def handler(sig):
                return None

            with pytest.warns(DeprecationWarning):
                getattr(dendrite, alias)(handler)

            # The canonical form registers the handler without warning.
            with warnings.catch_warnings():
                warnings.simplefilter("error", DeprecationWarning)
                getattr(dendrite, canonical)(handler)
        finally:
            await synapse.close()
    _run(run())


def test_detach_axon_emits_deregister_and_stops_hosting():
    from cosmonapse import Axon, SignalType

    async def run():
        synapse, dendrite = await _make_dendrite()
        deregs = []
        try:
            await synapse.subscribe(
                "cosmonapse.t.DEREGISTER", lambda s: deregs.append(s)
            )

            async def neuron(input, context):
                return {"ok": True}

            dendrite.attach_axon(Axon(neuron_id="a", neuron_fn=neuron))
            await dendrite.start()
            assert "a" in dendrite.axons

            await dendrite.detach_axon("a")
            await asyncio.sleep(0.01)

            # Axon is no longer hosted and a DEREGISTER went out for it.
            assert "a" not in dendrite.axons
            assert any((s.directed.id if s.directed else None) == "a" for s in deregs)
            assert all(s.type is SignalType.DEREGISTER for s in deregs)

            # stop() must not emit a second DEREGISTER for the detached Axon.
            before = len(deregs)
            await dendrite.stop()
            await asyncio.sleep(0.01)
            assert len(deregs) == before
        finally:
            await synapse.close()
    _run(run())


def test_detach_axon_unknown_raises_keyerror():
    async def run():
        synapse, dendrite = await _make_dendrite()
        try:
            with pytest.raises(KeyError):
                await dendrite.detach_axon("nope")
        finally:
            await synapse.close()
    _run(run())


def test_discover_response_is_skipped_after_stop():
    """Regression: respond_to_discover jitters up to 100ms before emitting
    REGISTER. A short-lived process can stop inside that window, and the
    publish then hit an already-closed Synapse and logged a warning."""
    from cosmonapse import Axon, discover_signal

    async def neuron(payload, context=None):
        return {"ok": True}

    async def run():
        synapse = MemorySynapse()
        await synapse.connect()
        d = Dendrite(synapse=synapse, namespace="t", role="worker")
        d.attach_axon(Axon(neuron_id="n", neuron_fn=neuron, capabilities=["c"]))
        await d.start()
        sig = discover_signal(neuron="n")
        task = asyncio.create_task(d.respond_to_discover(sig))
        await d.stop()
        await synapse.close()
        await task          # must return quietly, not raise or warn
        assert task.done() and task.exception() is None

    _run(run())


# ---------------------------------------------------------------------------
# Open calls: a TASK naming neither a neuron nor a capability
# ---------------------------------------------------------------------------
#
# The transport always supported this - _publish_task has a branch for it -
# but dispatch_task/dispatch used to refuse before reaching it. They no
# longer do, so the question "who answers?" moves entirely to the receiving
# side: a catch_all Axon, or an unfiltered @on_task_signal observer.


def test_open_call_is_dispatchable_and_unaddressed():
    from cosmonapse import SignalType

    async def run():
        synapse = MemorySynapse()
        await synapse.connect()
        d = Dendrite(synapse=synapse, namespace="t")
        await d.start()
        sig = await d.dispatch_task(input={"n": 1})
        assert sig.type is SignalType.TASK
        assert sig.directed is None
        assert not (sig.payload.get("capabilities") or [])
        await d.stop()
        await synapse.close()

    _run(run())


def test_only_a_catch_all_axon_answers_an_open_call():
    from cosmonapse import Axon

    async def build(synapse, *, catch_all):
        async def neuron(payload, context=None):
            return {"who": "me"}

        worker = Dendrite(synapse=synapse, namespace="t",
                          dendrite_id="w", role="worker")
        worker.attach_axon(Axon(neuron_id="n", neuron_fn=neuron,
                                capabilities=["c"], catch_all=catch_all))
        orch = Dendrite(synapse=synapse, namespace="t",
                        dendrite_id="o", heartbeat_s=0)
        await worker.start()
        await orch.start()
        return worker, orch

    async def run(catch_all):
        synapse = MemorySynapse()
        await synapse.connect()
        worker, orch = await build(synapse, catch_all=catch_all)
        try:
            sig = await orch.dispatch_and_wait(input={"n": 1}, timeout_s=1.0)
            return sig.payload.get("output")
        except asyncio.TimeoutError:
            return None
        finally:
            await worker.stop()
            await orch.stop()
            await synapse.close()

    assert _run(run(True)) == {"who": "me"}
    assert _run(run(False)) is None       # ordinary Axons stay deaf to it


def test_catch_all_does_not_widen_addressed_or_routed_delivery():
    """The flag touches the open-call branch only.

    A capability-routed TASK nobody matches must still be dropped: the routed
    subject is queue-grouped per cap profile, so answering out of group would
    break the once-only guarantee that routing exists to provide.
    """
    from cosmonapse import Axon

    async def run():
        synapse = MemorySynapse()
        await synapse.connect()

        async def neuron(payload, context=None):
            return {"who": "me"}

        worker = Dendrite(synapse=synapse, namespace="t",
                          dendrite_id="w", role="worker")
        worker.attach_axon(Axon(neuron_id="n", neuron_fn=neuron,
                                capabilities=["c"], catch_all=True))
        orch = Dendrite(synapse=synapse, namespace="t",
                        dendrite_id="o", heartbeat_s=0)
        await worker.start()
        await orch.start()
        try:
            # addressed to a neuron that is not hosted here -> still dropped
            with pytest.raises(asyncio.TimeoutError):
                await orch.dispatch_and_wait(
                    neuron="nobody", input={}, timeout_s=0.4,
                )
            # capability nobody covers -> still dropped
            with pytest.raises(asyncio.TimeoutError):
                await orch.dispatch_and_wait(
                    capabilities=["unmatched"], input={}, timeout_s=0.4,
                )
        finally:
            await worker.stop()
            await orch.stop()
            await synapse.close()

    _run(run())


def test_catch_all_is_advertised_on_register():
    """Discoverable, so "nobody answered" is distinguishable from
    "nobody was listening" without reading the source."""
    from cosmonapse import Axon, SignalType

    async def run():
        synapse = MemorySynapse()
        await synapse.connect()
        seen = []
        await synapse.subscribe(
            "cosmonapse.t.REGISTER",
            lambda sig: seen.append(sig),
        )

        async def neuron(payload, context=None):
            return {}

        d = Dendrite(synapse=synapse, namespace="t", role="worker")
        d.attach_axon(Axon(neuron_id="sponge", neuron_fn=neuron,
                           catch_all=True))
        d.attach_axon(Axon(neuron_id="plain", neuron_fn=neuron))
        await d.start()
        await asyncio.sleep(0.1)
        await d.stop()
        await synapse.close()

        meta = {s.directed.id: (s.meta or {}) for s in seen
                if s.type is SignalType.REGISTER and s.directed}
        assert meta["sponge"].get("catch_all") is True
        # absent, not False - an ordinary Axon says nothing about it
        assert "catch_all" not in meta["plain"]

    _run(run())
