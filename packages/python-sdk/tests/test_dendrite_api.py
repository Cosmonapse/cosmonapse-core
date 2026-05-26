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
                await dendrite.emit(register_signal(neuron="x", capabilities=[]))
            with pytest.raises(DendriteProtocolError):
                await dendrite.emit(heartbeat_signal(neuron="x"))
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
                               neuron="d", result={"ok": True})
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
