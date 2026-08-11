"""
cosmonapse.effector
~~~~~~~~~~~~~~~~~~~
Effector action layer for Cosmonapse: Neurons think, Engrams remember,
Effectors act.

Public surface:

  Effector             ABC every tool backend implements
  EffectorBinding      declarative wiring stored on an Axon
  EffectorClient       caller-side bridge over op-Pathways
  TOOL_STANDARDS       text tool-call dialect parsers (hermes/claude/codex/auto)
  ToolSchema           a tool's args, rendered into each provider's tools=
  tool_schema          build a ToolSchema from a Python function
  render_tools         schemas -> a provider's native tools= payload
  validate_args        check a parsed call against its schema
  extract_tool_calls   every call in a reply, structured channel first
  tool_result_messages observations -> the provider's own result turns
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
from cosmonapse.effector.schema import (
    ToolSchema,
    render_tools,
    tool_schema,
    validate_args,
)
from cosmonapse.effector.standards import (
    TOOL_STANDARDS,
    extract_native_calls,
    extract_tool_call,
    extract_tool_calls,
    tool_result_messages,
)

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
    "ToolSchema",
    "extract_native_calls",
    "extract_tool_call",
    "extract_tool_calls",
    "render_tools",
    "tool_result_messages",
    "tool_schema",
    "validate_args",
]
