"""
cosmonapse.effector
~~~~~~~~~~~~~~~~~~~
Effector action layer for Cosmonapse: Neurons think, Engrams remember,
Effectors act.

Public surface:

  Effector             ABC every tool backend implements
  EffectorBinding      declarative wiring stored on an Axon
  EffectorClient       caller-side bridge over op-Pathways
  TOOL_STANDARDS       native tool-call dialect parsers (hermes/claude/codex)
  ToolOutcome          what invoke() returns to the caller
  EffectorTimeout      deadline elapsed without a TOOL_RESULT
  EffectorCancelled    TASK terminated mid-call
  EffectorNotBound     Neuron asked for an unwired binding
  EffectorOverloaded   backend shed load
"""

from cosmonapse.effector.base import (
    Effector,
    EffectorBinding,
    EffectorCancelled,
    EffectorError,
    EffectorNotBound,
    EffectorOverloaded,
    EffectorTimeout,
    ToolOutcome,
)
from cosmonapse.effector.client import EffectorClient
from cosmonapse.effector.standards import TOOL_STANDARDS

__all__ = [
    "TOOL_STANDARDS",
    "Effector",
    "EffectorBinding",
    "EffectorCancelled",
    "EffectorClient",
    "EffectorError",
    "EffectorNotBound",
    "EffectorOverloaded",
    "EffectorTimeout",
    "ToolOutcome",
]
