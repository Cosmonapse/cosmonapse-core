"""
RegistryStore conformance suite.

`cosmonapse.storage.base` names this file as the single source of truth for
"what correct RegistryStore behaviour looks like." Every backend must pass it.

The in-memory and SQLite backends run with zero external dependencies. The
Postgres backend is exercised only when ``asyncpg`` is importable AND a
``COSMONAPSE_TEST_POSTGRES_DSN`` env var points at a reachable database;
otherwise it is skipped.
"""

import asyncio
import os
from datetime import datetime, timezone

import pytest

from cosmonapse.storage import (
    MemoryRegistryStore,
    NeuronRecord,
    SqliteRegistryStore,
)


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# Backends under test
# --------------------------------------------------------------------------

def _make_memory():
    return MemoryRegistryStore()


def _make_sqlite():
    # ":memory:" keeps a single connection for the life of the store instance.
    return SqliteRegistryStore(":memory:")


STORE_FACTORIES = [
    pytest.param(_make_memory, id="memory"),
    pytest.param(_make_sqlite, id="sqlite"),
]


# --------------------------------------------------------------------------
# Conformance tests (parametrized across every shipped backend)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("make_store", STORE_FACTORIES)
def test_upsert_then_get(make_store):
    async def run():
        store = make_store()
        await store.connect()
        try:
            await store.upsert(NeuronRecord(neuron_id="a", capabilities=["nlp"],
                                            version="1.0"))
            rec = await store.get("a")
            assert rec is not None
            assert rec.neuron_id == "a"
            assert rec.capabilities == ["nlp"]
            assert rec.version == "1.0"
            assert rec.status == "registered"
        finally:
            await store.close()
    _run(run())


@pytest.mark.parametrize("make_store", STORE_FACTORIES)
def test_get_unknown_returns_none(make_store):
    async def run():
        store = make_store()
        await store.connect()
        try:
            assert await store.get("does-not-exist") is None
        finally:
            await store.close()
    _run(run())


@pytest.mark.parametrize("make_store", STORE_FACTORIES)
def test_upsert_is_idempotent_update(make_store):
    async def run():
        store = make_store()
        await store.connect()
        try:
            await store.upsert(NeuronRecord(neuron_id="a", capabilities=["x"]))
            await store.upsert(NeuronRecord(neuron_id="a", capabilities=["x", "y"],
                                            version="2.0"))
            rec = await store.get("a")
            assert rec.capabilities == ["x", "y"]
            assert rec.version == "2.0"
            # Still exactly one record.
            assert len(await store.list()) == 1
        finally:
            await store.close()
    _run(run())


@pytest.mark.parametrize("make_store", STORE_FACTORIES)
def test_list_filters_by_capability(make_store):
    async def run():
        store = make_store()
        await store.connect()
        try:
            await store.upsert(NeuronRecord(neuron_id="a", capabilities=["nlp"]))
            await store.upsert(NeuronRecord(neuron_id="b", capabilities=["vision"]))
            nlp = await store.list(capability="nlp")
            assert {r.neuron_id for r in nlp} == {"a"}
        finally:
            await store.close()
    _run(run())


@pytest.mark.parametrize("make_store", STORE_FACTORIES)
def test_mark_deregistered_excluded_by_default(make_store):
    async def run():
        store = make_store()
        await store.connect()
        try:
            await store.upsert(NeuronRecord(neuron_id="a"))
            await store.mark_deregistered("a")
            assert await store.list() == []
            both = await store.list(include_deregistered=True)
            assert {r.neuron_id for r in both} == {"a"}
            assert (await store.get("a")).status == "deregistered"
        finally:
            await store.close()
    _run(run())


@pytest.mark.parametrize("make_store", STORE_FACTORIES)
def test_mark_deregistered_unknown_is_noop(make_store):
    async def run():
        store = make_store()
        await store.connect()
        try:
            await store.mark_deregistered("ghost")  # must not raise
            assert await store.get("ghost") is None
        finally:
            await store.close()
    _run(run())


@pytest.mark.parametrize("make_store", STORE_FACTORIES)
def test_touch_heartbeat_updates_existing(make_store):
    async def run():
        store = make_store()
        await store.connect()
        try:
            await store.upsert(NeuronRecord(neuron_id="a"))
            ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
            await store.touch_heartbeat("a", ts)
            rec = await store.get("a")
            assert rec.last_heartbeat is not None
            assert rec.last_heartbeat == ts
        finally:
            await store.close()
    _run(run())


@pytest.mark.parametrize("make_store", STORE_FACTORIES)
def test_touch_heartbeat_creates_thin_record(make_store):
    """A heartbeat arriving before REGISTER may create a thin record."""
    async def run():
        store = make_store()
        await store.connect()
        try:
            ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
            await store.touch_heartbeat("early", ts, status="registered")
            rec = await store.get("early")
            assert rec is not None
            assert rec.neuron_id == "early"
            assert rec.last_heartbeat == ts
        finally:
            await store.close()
    _run(run())


# --------------------------------------------------------------------------
# Postgres: only when both the driver and a live DSN are available
# --------------------------------------------------------------------------

def test_postgres_store_conformance():
    pytest.importorskip("asyncpg", reason="asyncpg not installed")
    dsn = os.environ.get("COSMONAPSE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("set COSMONAPSE_TEST_POSTGRES_DSN to run Postgres tests")

    from cosmonapse.storage import PostgresRegistryStore

    async def run():
        store = PostgresRegistryStore(dsn=dsn)
        await store.connect()
        try:
            await store.upsert(NeuronRecord(neuron_id="pg-a", capabilities=["nlp"]))
            rec = await store.get("pg-a")
            assert rec is not None and rec.capabilities == ["nlp"]
            await store.mark_deregistered("pg-a")
            assert await store.list() == [] or all(
                r.neuron_id != "pg-a" for r in await store.list()
            )
        finally:
            await store.close()
    _run(run())
