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

#: Banner suppression, process-wide. See `set_quiet`.
_QUIET = False


def set_quiet(value: bool) -> None:
    """Silence banner output for the rest of the process.

    Genesis spawns `cosmo synapse start --quiet` with stdout pointed at the
    null device, where a banner is not merely unread  -  it is the thing most
    likely to raise on the way out, taking a healthy server with it. Gating
    once at the writer keeps `--quiet` meaning the same thing for every
    transport instead of asking each starter to remember to check. Errors are
    deliberately unaffected: `_err` writes to stderr and always speaks.
    """
    global _QUIET
    _QUIET = bool(value)


def _stdout_is_console() -> bool:
    """Is stdout a terminal we can drive, rather than a file or a null sink?"""
    try:
        return bool(sys.stdout is not None and sys.stdout.isatty())
    except Exception:  # detached or closed stream
        return False


def _rule_char() -> str:
    """``-`` unless stdout can actually encode a box-drawing dash.

    A redirected stdout on Windows is opened with the locale encoding, and
    cp1252 has no U+2500. Writing one raises UnicodeEncodeError from inside a
    banner, which is a spectacular way to lose a server process over
    decoration. Ask the stream what it can represent instead of assuming.
    """
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        "\u2500".encode(enc)
    except (LookupError, UnicodeEncodeError, TypeError):
        return "-"
    return "\u2500"


if _HAS_RICH:
    # `legacy_windows=False` when stdout is not a console, and it matters.
    #
    # On Windows rich decides whether to use the pre-VT Win32 console renderer
    # by calling GetConsoleMode on fd 1. Redirect stdout to NUL  -  which is
    # exactly what Genesis does when it spawns `cosmo synapse start`  -  and
    # that call fails, so rich concludes it is talking to an old console and
    # drives the Win32 console API against a handle with no console behind it.
    # The write raises, and a synapse dies mid-banner having already bound its
    # port and registered its namespace. Not a tty means there is no console to
    # drive, so say so outright rather than letting detection guess wrong.
    _console = Console() if _stdout_is_console() else Console(legacy_windows=False)

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
        if _QUIET:
            return
        _console.print("  " + _rule_char() * 64)

    def _banner_line(text: str, style: str = "") -> None:
        if _QUIET:
            return
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
        if _QUIET:
            return
        print("  " + _rule_char() * 64)

    def _banner_line(text: str, style: str = "") -> None:
        if _QUIET:
            return
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
    "_rule_char",
    "_stdout_is_console",
    "set_quiet",
]
