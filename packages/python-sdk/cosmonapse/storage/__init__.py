"""
cosmonapse.storage
~~~~~~~~~~~~~~~~~~
Pluggable local-data adapters for Dendrite / Cortex.

RegistryStore is the only mandatory store the SDK ships. Cortex uses it
to track every Neuron it has seen on the namespace (REGISTER /
HEARTBEAT / DEREGISTER → capabilities, status, version, last_heartbeat).

For anything else a developer wants to persist (costs, latency, model
mix, audit history), subscribe to the Synapse and write your own
helpers. The SDK deliberately stops at RegistryStore so the surface
stays small.

Backends shipped:

  MemoryRegistryStore     in-process; default if you don't pass one
  SqliteRegistryStore     stdlib sqlite3; zero deps, single-file DB
  PostgresRegistryStore   asyncpg (lazy-imported); for real deployments
"""

from cosmonapse.storage.base import NeuronRecord, RegistryStore
from cosmonapse.storage.memory import MemoryRegistryStore
from cosmonapse.storage.sqlite import SqliteRegistryStore
from cosmonapse.storage.postgres import PostgresRegistryStore

__all__ = [
    "NeuronRecord",
    "RegistryStore",
    "MemoryRegistryStore",
    "SqliteRegistryStore",
    "PostgresRegistryStore",
]
