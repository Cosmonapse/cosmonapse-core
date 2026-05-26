"""
cosmonapse.storage.memory
~~~~~~~~~~~~~~~~~~~~~~~~~
In-process RegistryStore. Default backend if none is supplied.

State is held in a plain dict, so it is reset every time the process
restarts. Use this for tests, local dev, and ephemeral Cortices that
genuinely shouldn't survive a crash.
"""

from __future__ import annotations

from datetime import datetime

from cosmonapse.storage.base import NeuronRecord, RegistryStore


class MemoryRegistryStore(RegistryStore):
    def __init__(self) -> None:
        self._records: dict[str, NeuronRecord] = {}

    async def connect(self) -> None:
        return

    async def close(self) -> None:
        self._records.clear()

    async def upsert(self, record: NeuronRecord) -> None:
        # Preserve the original registered_at if we've seen this neuron before.
        existing = self._records.get(record.neuron_id)
        if existing is not None:
            record.registered_at = existing.registered_at
        self._records[record.neuron_id] = record

    async def mark_deregistered(self, neuron_id: str) -> None:
        rec = self._records.get(neuron_id)
        if rec is not None:
            rec.status = "deregistered"

    async def touch_heartbeat(
        self,
        neuron_id: str,
        ts: datetime,
        status: str | None = None,
    ) -> None:
        rec = self._records.get(neuron_id)
        if rec is None:
            rec = NeuronRecord(neuron_id=neuron_id, last_heartbeat=ts)
            if status:
                rec.status = status
            self._records[neuron_id] = rec
            return
        rec.last_heartbeat = ts
        if status:
            rec.status = status

    async def get(self, neuron_id: str) -> NeuronRecord | None:
        return self._records.get(neuron_id)

    async def list(
        self,
        *,
        capability: str | None = None,
        include_deregistered: bool = False,
    ) -> list[NeuronRecord]:
        out: list[NeuronRecord] = []
        for rec in self._records.values():
            if not include_deregistered and rec.status == "deregistered":
                continue
            if capability is not None and capability not in rec.capabilities:
                continue
            out.append(rec)
        return out
