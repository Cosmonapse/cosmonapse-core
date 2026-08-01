"""
cosmonapse.receptor
~~~~~~~~~~~~~~~~~~~
Receptor interface layer for Cosmonapse: Neurons think, Engrams remember,
Effectors act, Receptors listen.

A Receptor is the edge where the outside world touches the fabric. It
collects something - a typed command, an HTTP request, a chat turn -
builds a TASK from it, and hands the trace back in one of three shapes,
which are the three that Dendrite already offers:

    send    -> dispatch_task            fire-and-forget
    wait    -> dispatch_and_wait        request / reply
    stream  -> dispatch_and_subscribe   live event stream

Nothing new crosses the wire. A Receptor emits the same TASK an
orchestrator Dendrite always emitted, so `cosmo prism` sees no new
signal types and an existing brain needs no changes to grow an interface.

Public surface:

  Receptor             base class every interface backend extends
  CliReceptor          terminal: a command becomes a TASK
  ApiReceptor          HTTP: one endpoint, all three shapes
  ChatReceptor         conversation: one turn, one dispatch (+ voice)
  run_brain            run a whole brain: every Dendrite, every interface
  run_receptors        run several Receptors at once (Dendrite.run uses it)
  idle                 block forever - a brain with no interface still runs
  DispatchMode         "send" | "wait" | "stream"
  ReceptorError        trace ended on ERROR
  ReceptorTimeout      no terminal Signal inside the deadline
  ReceptorUnbound      no neuron= / capabilities= target

``ApiReceptor`` and ``ChatReceptor`` need FastAPI, which is not a core
dependency: ``pip install 'cosmonapse[receptor]'``. They are imported
lazily here, so ``import cosmonapse`` costs nothing without it.
"""

from typing import TYPE_CHECKING, Any

from cosmonapse.receptor.base import (
    TERMINAL_TYPES,
    DispatchMode,
    Receptor,
    ReceptorError,
    ReceptorTimeout,
    ReceptorUnbound,
    signal_to_jsonable,
)
from cosmonapse.receptor.cli import CliReceptor, Command
from cosmonapse.receptor.runner import idle, run_brain, run_receptors

if TYPE_CHECKING:  # pragma: no cover - typing only
    from cosmonapse.receptor.api import ApiReceptor
    from cosmonapse.receptor.chat import ChatReceptor

__all__ = [
    "TERMINAL_TYPES",
    "ApiReceptor",
    "ChatReceptor",
    "CliReceptor",
    "Command",
    "DispatchMode",
    "Receptor",
    "ReceptorError",
    "ReceptorTimeout",
    "ReceptorUnbound",
    "idle",
    "run_brain",
    "run_receptors",
    "signal_to_jsonable",
]

_LAZY = {
    "ApiReceptor": ("cosmonapse.receptor.api", "ApiReceptor"),
    "ChatReceptor": ("cosmonapse.receptor.chat", "ChatReceptor"),
}


def __getattr__(name: str) -> Any:
    """Import the FastAPI-backed Receptors only when they are asked for."""
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(target[0]), target[1])


def __dir__() -> list[str]:
    return sorted(__all__)
