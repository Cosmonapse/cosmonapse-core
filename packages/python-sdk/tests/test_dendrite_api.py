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
