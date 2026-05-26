"""
cosmonapse._url
~~~~~~~~~~~~~~~
Synapse URL -> Synapse factory and connector.

A Dendrite (or Cortex) does NOT own the Synapse. It uses a Synapse that
the caller has built and connected. The caller is also responsible for
closing it.

  cosmo://host:port      -> DevSynapse (local dev / cosmo dev synapse)
  nats://host:port       -> NatsSynapse
  kafka://host:port      -> KafkaSynapse

For in-process MemorySynapse, construct it directly — a URL would be
ambiguous across processes.
"""

from __future__ import annotations

from urllib.parse import urlparse

from cosmonapse.synapse.base import Synapse


def synapse_from_url(url: str) -> Synapse:
    """Build (but do not connect) a Synapse from a Cosmonapse synapse URL."""
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()

    if scheme == "cosmo":
        from cosmonapse.synapse.dev import DevSynapse
        return DevSynapse(
            host=parsed.hostname or "127.0.0.1",
            port=parsed.port or 7070,
        )
    if scheme == "nats":
        from cosmonapse.synapse.nats import NatsSynapse
        return NatsSynapse(url=url)
    if scheme == "kafka":
        from cosmonapse.synapse.kafka import KafkaSynapse
        host = parsed.hostname or "localhost"
        port = parsed.port or 9092
        return KafkaSynapse(bootstrap_servers=f"{host}:{port}")

    raise ValueError(
        f"Unknown synapse URL scheme {scheme!r}. "
        f"Expected one of: cosmo, nats, kafka. "
        f"For in-process MemorySynapse, instantiate it directly."
    )


async def connect_synapse(url: str) -> Synapse:
    """
    Build a Synapse from `url` and `.connect()` it. Return the
    connected Synapse so the caller can pass it to Dendrites / Cortices
    and close it when finished:

        synapse = await connect_synapse("cosmo://127.0.0.1:7070")
        try:
            dendrite = Dendrite(synapse=synapse, registry_store=...)
            async with dendrite:
                ...
        finally:
            await synapse.close()

    Multiple Dendrites and Cortices can share the same Synapse instance.
    Closing the Synapse is the caller's responsibility — no component
    will close it on you.
    """
    t = synapse_from_url(url)
    await t.connect()
    return t
