"""
cosmo.commands._shared
~~~~~~~~~~~~~~~~~~~~~~~~
Shared CLI utilities used across the ``cosmo`` command tree.

Signal pretty-printing (and the colour map that drives it) used to be copy
-pasted into every command module. It now lives here so there is exactly one
definition to keep in sync. Both the rich-backed and the plain-text variants
are exported through the same names, picked at import time based on whether
``rich`` is installed.
"""

from __future__ import annotations

import sys

from cosmonapse import Signal

try:
    from rich.console import Console
    from rich.text import Text

    _HAS_RICH = True
except ImportError:  # pragma: no cover - exercised only without rich
    _HAS_RICH = False


# ---------------------------------------------------------------------------
# Signal-type → rich style map
# ---------------------------------------------------------------------------

_TYPE_COLOURS: dict[str, str] = {
    "TASK": "cyan",
    "AGENT_OUTPUT": "green",
    "FINAL": "bold green",
    "ERROR": "bold red",
    "CLARIFICATION": "yellow",
    "REGISTER": "blue",
    "DEREGISTER": "blue",
    "HEARTBEAT": "dim blue",
    "TASK_OFFER": "magenta",
    "BID": "magenta",
    "TASK_AWARDED": "bold magenta",
    "TASK_DECLINED": "dim magenta",
    "THOUGHT_DELTA": "dim white",
    "PLAN": "white",
    "TOOL_CALL": "bright_white",
    "TOOL_RESULT": "bright_white",
    "MEMORY_APPEND": "bright_cyan",
    "ESCALATION": "bold yellow",
    "CONSENSUS": "bold cyan",
    "CONTEXT_SYNC": "cyan",
    "CRITIQUE": "yellow",
    # Engram
    "RECALL": "bright_cyan",
    "RECALLED": "bright_cyan",
    "IMPRINT": "cyan",
    "IMPRINTED": "cyan",
    # Clarification / permission
    "PERMISSION": "yellow",
    "PERMISSION_DECISION": "dim yellow",
    "CLARIFICATION_ANSWER": "dim yellow",
}


# ---------------------------------------------------------------------------
# Output helpers  -  rich variant if available, plain otherwise
# ---------------------------------------------------------------------------

if _HAS_RICH:
    _console = Console()

    def _print_signal(subject: str, sig: Signal) -> None:
        colour = _TYPE_COLOURS.get(sig.type.value, "white")
        ts = sig.ts.strftime("%H:%M:%S.%f")[:-3]
        neuron = (sig.directed.id if sig.directed else None) or " - "
        trace = sig.trace_id[4:12]
        t = Text()
        t.append(f"  {ts}  ", style="dim")
        t.append(f"{sig.type.value:<14}", style=colour)
        t.append(f"  {trace}  ", style="dim")
        t.append(f"{neuron:<18}", style="italic")
        t.append(f"  {subject}", style="dim")
        _console.print(t)

    def _hr() -> None:
        _console.print("  " + "─" * 64)

    def _banner_line(text: str, style: str = "") -> None:
        _console.print(text, style=style)

    def _err(text: str) -> None:
        _console.print(text, style="bold red")

else:

    def _print_signal(subject: str, sig: Signal) -> None:
        ts = sig.ts.strftime("%H:%M:%S")
        print(
            f"  {ts}  {sig.type.value:<14}  {sig.trace_id[4:12]}  "
            f"{((sig.directed.id if sig.directed else None) or ' - '):<18}  {subject}"
        )

    def _hr() -> None:
        print("  " + "─" * 64)

    def _banner_line(text: str, style: str = "") -> None:
        print(text)

    def _err(text: str) -> None:
        print(text, file=sys.stderr)


__all__ = [
    "_HAS_RICH",
    "_TYPE_COLOURS",
    "_banner_line",
    "_err",
    "_hr",
    "_print_signal",
]
