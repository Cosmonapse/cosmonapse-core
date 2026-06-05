"""
Tests for the DevSynapse client/server and request/reply.

Covers the cleanup fixes:
  * DevSynapse(port=0) must request an OS-assigned port, not fall back to 7070.
  * request/reply uses get_running_loop() (no event-loop DeprecationWarning).
"""

import asyncio
import warnings


from cosmonapse import DevSynapse, DevSynapseServer, MemorySynapse, task_signal


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# port handling
# --------------------------------------------------------------------------

def test_port_zero_is_preserved_via_kwarg():
    s = DevSynapse(port=0)
    assert s._port == 0  # 0 == "let the OS choose", must not become 7070


def test_port_zero_is_preserved_via_url():
    s = DevSynapse(url="cosmo://127.0.0.1:0")
    assert s._port == 0


def test_port_defaults_to_7070_when_unset():
    assert DevSynapse()._port == 7070
    # A URL with no explicit port also falls back to 7070.
    assert DevSynapse(url="cosmo://127.0.0.1")._port == 7070


def test_on_signal_default_is_none():
    server = DevSynapseServer()
    assert server.on_signal is None


# --------------------------------------------------------------------------
# end-to-end roundtrip over a real OS-assigned port
# --------------------------------------------------------------------------

def test_dev_synapse_pub_sub_roundtrip():
    received = []

    async def run():
        server = DevSynapseServer(port=0)
        await server.start()
        assert server.port != 0  # OS assigned a real port
        client = DevSynapse(url=server.url)
        await client.connect()
        try:
            await client.subscribe("cosmonapse.dev.>", lambda s: received.append(s))
            await asyncio.sleep(0.05)
            await client.publish("cosmonapse.dev.TASK", task_signal(input={"x": 1}))
            await asyncio.sleep(0.1)
        finally:
            await client.close()
            await server.stop()

    _run(run())
    assert len(received) == 1
    assert received[0].payload["input"] == {"x": 1}


# --------------------------------------------------------------------------
# request/reply must not emit the get_event_loop DeprecationWarning
# --------------------------------------------------------------------------

def test_memory_request_reply_no_event_loop_warning():
    async def run():
        synapse = MemorySynapse()
        await synapse.connect()

        async def responder(sig):
            reply_to = sig.meta.get("_reply_to")
            if reply_to:
                await synapse.publish(reply_to, sig.reply(
                    type=sig.type, payload={"echo": sig.payload}))

        await synapse.subscribe("cosmonapse.r.TASK", responder)
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            reply = await synapse.request(
                "cosmonapse.r.TASK", task_signal(input={"ping": 1}), timeout_s=2.0)
        await synapse.close()
        return reply

    reply = _run(run())
    assert reply is not None
