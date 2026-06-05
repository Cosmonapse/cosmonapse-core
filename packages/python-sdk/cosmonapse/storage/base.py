"""
cosmonapse.storage.base
~~~~~~~~~~~~~~~~~~~~~~~
The mandatory store: RegistryStore.

Every backend must implement this exact interface. The conformance
suite in tests/test_registry_store.py runs against any RegistryStore
and is the single source of truth for "what correct behaviour looks
like." A new backend (Redis, DynamoDB, anything) is conformant iff it
passes that suite.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class NeuronRecord:
    """A live view of one Neuron the namespace has seen."""

    neuron_id: str
    capabilities: list[str] = field(default_factory=list)
    version: str | None = None
    status: str = "registered"          # "registered" | "draining" | "deregistered"
    last_heartbeat: datetime | None = None
    registered_at: datetime = field(default_factory=_now_utc)

    def to_dict(self) -> dict[str, Any]:
        return {
            "neuron_id": self.neuron_id,
            "capabilities": list(self.capabilities),
            "version": self.version,
            "status": self.status,
            "last_heartbeat": (
                self.last_heartbeat.isoformat() if self.last_heartbeat else None
            ),
            "registered_at": self.registered_at.isoformat(),
        }


class RegistryStore(ABC):
    """
    Abstract registry store. All methods are async  -  backends that wrap
    sync libraries (sqlite3) must dispatch to a threadpool internally
    so the event loop never blocks.

    Implementations are expected to be safe for concurrent calls from
    multiple coroutines on the same event loop. They are NOT required
    to be safe across processes  -  Postgres is the canonical choice
    when multiple Cortex processes share state.
    """

    @abstractmethod
    async def connect(self) -> None:
        """Open underlying resources (DB pool, file handle, …)."""

    @abstractmethod
    async def close(self) -> None:
        """Release underlying resources."""

    @abstractmethod
    async def upsert(self, record: NeuronRecord) -> None:
        """
        Insert or update a NeuronRecord by `neuron_id`.

        Called on REGISTER. Replaces capabilities / version / status /
        last_heartbeat / registered_at with the supplied values.
        """

    @abstractmethod
    async def mark_deregistered(self, neuron_id: str) -> None:
        """
        Mark an existing record as deregistered. If no record exists,
        the call is a no-op.
        """

    @abstractmethod
    async def touch_heartbeat(
        self,
        neuron_id: str,
        ts: datetime,
        status: str | None = None,
    ) -> None:
        """
        Update last_heartbeat (and optionally status) for an existing
        record. If no record exists, the backend MAY create a thin
        record with just neuron_id + last_heartbeat  -  this matches how
        the Cortex tolerates heartbeats arriving before REGISTER.
        """

    @abstractmethod
    async def get(self, neuron_id: str) -> NeuronRecord | None:
        """Return the record for `neuron_id`, or None if unknown."""

    @abstractmethod
    async def list(
        self,
        *,
        capability: str | None = None,
        include_deregistered: bool = False,
    ) -> list[NeuronRecord]:
        """
        Return records, optionally filtered by capability and/or
        excluding records whose status is "deregistered" (the default).
        """
