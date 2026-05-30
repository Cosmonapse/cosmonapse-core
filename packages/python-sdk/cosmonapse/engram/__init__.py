"""
cosmonapse.engram
~~~~~~~~~~~~~~~~~
Engram storage layer for Cosmonapse. See ENGRAM_DESIGN.md.

Public surface:

  Engram               ABC every backend implements
  EngramBinding        declarative wiring stored on an Axon
  Hit                  one search result
  RecallResult         what recall() returns to the caller
  ImprintReceipt       what imprint() returns to the caller
  EngramTimeout        deadline elapsed without a response
  EngramCancelled      TASK terminated mid-call
  EngramNotBound       Neuron asked for an unwired binding
  EngramOverloaded     backend shed load

Backends:

  InMemoryEngram   dict-backed, default for tests/dev
  SqliteEngram     stdlib sqlite3, single-file DB
  PostgresEngram   asyncpg (lazy-imported); for real deployments
"""

from cosmonapse.engram.base import (
    Engram,
    EngramBinding,
    EngramCancelled,
    EngramError,
    EngramNotBound,
    EngramOverloaded,
    EngramTimeout,
    Hit,
    ImprintReceipt,
    RecallResult,
)
from cosmonapse.engram.client import EngramClient
from cosmonapse.engram.memory import InMemoryEngram
from cosmonapse.engram.postgres import PostgresEngram
from cosmonapse.engram.sqlite import SqliteEngram

__all__ = [
    "Engram",
    "EngramBinding",
    "EngramCancelled",
    "EngramClient",
    "EngramError",
    "EngramNotBound",
    "EngramOverloaded",
    "EngramTimeout",
    "Hit",
    "ImprintReceipt",
    "InMemoryEngram",
    "PostgresEngram",
    "RecallResult",
    "SqliteEngram",
]
