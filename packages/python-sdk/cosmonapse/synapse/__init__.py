"""
cosmonapse.synapse
~~~~~~~~~~~~~~~~~~~~
Synapse adapters.

  MemorySynapse       in-process; for tests and tightly-coupled callers.
  DevSynapse   TCP + NDJSON; the client side of `cosmo dev synapse`.
                        Production-grade for multi-process dev on one host.
  NatsSynapse         production default; clean fit for the protocol.
  KafkaSynapse        durable, audit-friendly; trickier request/reply.

NatsSynapse and KafkaSynapse lazy-import their client libraries.
The modules themselves import safely without nats-py / aiokafka.
connect() raises a clear ImportError if the dep is missing.
"""

from cosmonapse.synapse.base import MessageHandler, Subscription, Synapse
from cosmonapse.synapse.dev import DevSynapse, DevSynapseServer
from cosmonapse.synapse.kafka import KafkaSynapse
from cosmonapse.synapse.memory import MemorySynapse
from cosmonapse.synapse.nats import NatsSynapse

__all__ = [
    "DevSynapse",
    "DevSynapseServer",
    "KafkaSynapse",
    "MemorySynapse",
    "MessageHandler",
    "NatsSynapse",
    "Subscription",
    "Synapse",
]
