"""
cosmonapse.receptor.cli
~~~~~~~~~~~~~~~~~~~~~~~
The terminal Receptor: a command becomes a TASK.

``CliReceptor`` builds the argparse tree *and* the REPL from the command
functions you declare, so the ~150 lines of argparse / input() / result
formatting every example's ``cli.py`` hand-rolls collapse to::

    rx = CliReceptor(dendrite=orch, prog="agent", input_key="goal")

    @rx.command()                       # the default command
    async def run(goal: str, max_steps: int = 6):
        return {"goal": goal, "max_steps": max_steps}

    @rx.command("memory", local=True)   # answered locally, no dispatch
    async def memory():
        return agent_memory.summary()

    await rx.run()

That gives you, for free:

    python cli.py "some goal"          one-shot   -> dispatch_and_wait
    python cli.py --stream "some goal" one-shot   -> dispatch_and_subscribe
    python cli.py --send "some goal"   one-shot   -> dispatch_task
    python cli.py memory               local command
    python cli.py                      interactive REPL (:memory, :help, :quit)

A command function returns the TASK ``input`` dict (or a string, wrapped
with ``input_key``). ``local=True`` marks a command that never dispatches -
its return value is printed as-is; that is the ``:memory`` / ``:stats``
shape every example already has.

Parameters become CLI arguments by signature: no default -> positional,
default -> ``--flag``, ``bool`` default -> ``--flag`` / store_true.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from cosmonapse.envelope import Signal, SignalType
from cosmonapse.receptor.base import (
    DispatchMode,
    Receptor,
    ReceptorError,
    ReceptorTimeout,
    _maybe_await,
)

DIM, BOLD, RED, RESET = "\033[2m", "\033[1m", "\033[31m", "\033[0m"


@dataclass
class Command:
    """One declared command: the argparse spec and the input builder."""

    name: str
    fn: Callable[..., Any]
    help: str = ""
    local: bool = False
    mode: DispatchMode | None = None
    is_default: bool = False
    params: list[inspect.Parameter] = field(default_factory=list)

    def add_to(self, parser: argparse.ArgumentParser) -> None:
        for p in self.params:
            _add_param(parser, p)

    def kwargs_from(self, ns: argparse.Namespace) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for p in self.params:
            if not hasattr(ns, p.name):
                continue
            value = getattr(ns, p.name)
            # A required str positional is collected with nargs="+" so a
            # multi-word request needs no quoting; the function wants one
            # string, so join it back.
            if isinstance(value, list) and _is_text_positional(p):
                value = " ".join(value)
            out[p.name] = value
        return out


def _param_kind(p: inspect.Parameter) -> type:
    ann = p.annotation if p.annotation is not inspect.Parameter.empty else str
    return ann if ann in (str, int, float, bool) else str


def _is_text_positional(p: inspect.Parameter) -> bool:
    return (p.default is inspect.Parameter.empty
            and _param_kind(p) is str)


def _add_param(parser: argparse.ArgumentParser, p: inspect.Parameter) -> None:
    kind = _param_kind(p)
    if p.default is inspect.Parameter.empty:
        # Required -> positional. A str one takes nargs="+" so a multi-word
        # request ("research the Collatz conjecture") needs no quoting;
        # kwargs_from joins it back into a single string.
        if kind is str:
            parser.add_argument(p.name, nargs="+")
        else:
            parser.add_argument(p.name, type=kind)
        return
    flag = "--" + p.name.replace("_", "-")
    if kind is bool:
        parser.add_argument(flag, dest=p.name, action="store_true",
                            default=p.default)
    else:
        parser.add_argument(flag, dest=p.name, type=kind, default=p.default,
                            help=f"(default {p.default})")


class CliReceptor(Receptor):
    """Terminal interface onto the dispatch trio.

    ``neuron`` and ``capabilities`` are both optional and both may be set
    per call instead - see :class:`~cosmonapse.receptor.base.Receptor`.

    ``mode`` is the default shape (``"wait"``); ``--stream`` and ``--send``
    override it per invocation. Everything the base Receptor offers -
    ``on_input``, ``on_result``, ``on_signal`` - works here, and
    ``on_signal`` is how you get a live trace view in the terminal.
    """

    def __init__(
        self,
        *,
        dendrite=None,
        neuron: str | None = None,
        capabilities: list[str] | None = None,
        prog: str | None = None,
        description: str | None = None,
        banner: str | None = None,
        prompt: str = "> ",
        mode: DispatchMode = "wait",
        **kw: Any,
    ) -> None:
        kw.setdefault("receptor_id", prog or "cli-receptor")
        super().__init__(dendrite=dendrite, neuron=neuron,
                         capabilities=capabilities, **kw)
        self.default_mode = mode
        self._prog = prog or "cli"
        self._description = description or "A Cosmonapse interface."
        self._banner = banner
        self._prompt = prompt
        self._commands: dict[str, Command] = {}
        self._default: Command | None = None
        self._printer: Callable[[Any], Any] | None = None

    # ------------------------------------------------------------------
    # Declaring commands
    # ------------------------------------------------------------------

    def command(
        self, name: str | None = None, *, help: str = "",
        local: bool = False, mode: DispatchMode | None = None,
        default: bool | None = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Declare a command. Undecorated name -> the function's name.

        ``default=True`` (implicit for the first non-local command) makes
        it the one that runs when the user types a bare goal.
        """
        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            cmd_name = name or fn.__name__.replace("_", "-")
            sig = inspect.signature(fn)
            params = []
            for p in sig.parameters.values():
                if p.kind in (inspect.Parameter.VAR_POSITIONAL,
                              inspect.Parameter.VAR_KEYWORD):
                    raise TypeError(
                        f"command {cmd_name!r}: *args / **kwargs are not "
                        f"supported - declare named parameters, they become "
                        f"the CLI arguments"
                    )
                params.append(p)
            is_default = default
            if is_default is None:
                is_default = not local and self._default is None
            cmd = Command(
                name=cmd_name, fn=fn, help=help or (fn.__doc__ or "").strip(),
                local=local, mode=mode, is_default=bool(is_default),
                params=params,
            )
            self._commands[cmd_name] = cmd
            if cmd.is_default:
                self._default = cmd
            return fn
        return decorator

    def on_print(self, fn: Callable[[Any], Any]) -> Callable[[Any], Any]:
        """How a rendered result is written to the terminal. Sync or async."""
        self._printer = fn
        return fn

    # ------------------------------------------------------------------
    # Running
    # ------------------------------------------------------------------

    def parser(self) -> argparse.ArgumentParser:
        """The argparse tree built from the declared commands."""
        ap = argparse.ArgumentParser(prog=self._prog,
                                     description=self._description)
        ap.add_argument("--stream", action="store_true",
                        help="stream every Signal on the trace as it arrives")
        ap.add_argument("--send", action="store_true",
                        help="fire-and-forget: emit the TASK and exit")
        ap.add_argument("--timeout", type=float, default=self._timeout_s,
                        help=f"per-run timeout in seconds "
                             f"(default {self._timeout_s})")
        ap.add_argument("--json", action="store_true",
                        help="print the raw result as JSON")
        if not self._commands:
            return ap
        subs = ap.add_subparsers(dest="_command")
        for cmd in self._commands.values():
            sp = subs.add_parser(cmd.name, help=cmd.help)
            cmd.add_to(sp)
        return ap

    def owns_terminal(self, argv: list[str] | None = None) -> bool:
        """No command on argv -> ``run()`` drops into the REPL.

        Deliberately the same "first non-flag token" test ``run()`` makes,
        so the runner's view of this Receptor and the Receptor's own view
        cannot drift.
        """
        argv = list(sys.argv[1:] if argv is None else argv)
        return next((a for a in argv if not a.startswith("-")), None) is None

    async def run(self, argv: list[str] | None = None) -> int:
        """Parse argv and either run one command or drop into the REPL."""
        argv = list(sys.argv[1:] if argv is None else argv)
        ap = self.parser()
        known = set(self._commands)
        # A bare goal ("cli.py research X") is the default command with its
        # first positional filled in - insert the name so argparse agrees.
        head = next((a for a in argv if not a.startswith("-")), None)
        if head is not None and head not in known and self._default is not None:
            argv.insert(argv.index(head), self._default.name)
        try:
            ns = ap.parse_args(argv)
        except SystemExit as exc:
            # --help, or a usage error. argparse has already printed; this
            # was a command-line invocation, so the process is done.
            self.ends_process = True
            return int(exc.code or 0)

        mode: DispatchMode = ("stream" if ns.stream
                              else "send" if ns.send else self.default_mode)
        name = getattr(ns, "_command", None)
        if not name:
            # No command on argv: this is the interactive REPL, a long-lived
            # interface. `:quit` closes it and leaves the brain running.
            return await self.repl(mode=mode, timeout_s=ns.timeout,
                                   as_json=ns.json)
        # A named command: one shot, print, done. The invocation ends with
        # it, so tell the runner not to fall through to idling.
        self.ends_process = True
        cmd = self._commands[name]
        return await self._run_command(cmd, cmd.kwargs_from(ns), mode=mode,
                                       timeout_s=ns.timeout, as_json=ns.json)

    async def repl(
        self, *, mode: DispatchMode = "wait", timeout_s: float | None = None,
        as_json: bool = False,
    ) -> int:
        """Interactive loop. ``:name`` runs a declared command; anything
        else goes to the default command as its first positional."""
        print(self._banner or f"{BOLD}{self._prog}{RESET} - "
                              f"type a request, or :help")
        loop = asyncio.get_running_loop()
        while True:
            try:
                line = await loop.run_in_executor(None, input, self._prompt)
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            text = line.strip()
            if not text:
                continue
            if text in (":q", ":quit", ":exit"):
                return 0
            if text in (":h", ":help", "?"):
                print(self._help_text())
                continue
            if text.startswith(":"):
                head, _, rest = text[1:].partition(" ")
                cmd = self._commands.get(head)
                if cmd is None:
                    print(f"unknown command :{head} - try :help")
                    continue
                kwargs = _repl_kwargs(cmd, rest)
            else:
                if self._default is None:
                    print("no default command - try :help")
                    continue
                cmd, kwargs = self._default, _repl_kwargs(self._default, text)
            await self._run_command(cmd, kwargs, mode=mode, timeout_s=timeout_s,
                                    as_json=as_json)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _run_command(
        self, cmd: Command, kwargs: dict[str, Any], *, mode: DispatchMode,
        timeout_s: float | None, as_json: bool,
    ) -> int:
        try:
            raw = await _maybe_await(cmd.fn(**kwargs))
        except TypeError as exc:
            print(f"{RED}! {cmd.name}: {exc}{RESET}", file=sys.stderr)
            return 2
        if cmd.local:
            await self._print(raw, as_json=as_json)
            return 0

        chosen = cmd.mode or mode
        try:
            if chosen == "stream":
                async for sig in self.iter_signals(raw, timeout_s=timeout_s):
                    self._print_signal(sig)
                return 0
            result = await self.receive(raw, mode=chosen, timeout_s=timeout_s)
        except ReceptorTimeout:
            print(f"{RED}! timed out{RESET}", file=sys.stderr)
            return 1
        except ReceptorError as exc:
            print(f"{RED}! {exc}{RESET}", file=sys.stderr)
            return 1
        if isinstance(result, Signal):     # send mode
            print(f"{DIM}dispatched  trace {result.trace_id}{RESET}")
            return 0
        await self._print(result, as_json=as_json)
        return 0

    async def _print(self, value: Any, *, as_json: bool) -> None:
        if self._printer is not None and not as_json:
            await _maybe_await(self._printer(value))
            return
        if as_json or not isinstance(value, str):
            print(json.dumps(value, indent=2, default=str))
        else:
            print(value)

    def _print_signal(self, sig: Signal) -> None:
        if sig.type in (SignalType.FINAL, SignalType.AGENT_OUTPUT):
            body = (sig.payload or {}).get("output", sig.payload)
            print(f"{BOLD}{sig.type.value}{RESET} "
                  f"{json.dumps(body, indent=2, default=str)}")
        elif sig.type is SignalType.ERROR:
            print(f"{RED}ERROR {(sig.payload or {}).get('message')}{RESET}",
                  file=sys.stderr)
        else:
            brief = json.dumps(sig.payload or {}, default=str)[:120]
            print(f"{DIM}  {sig.type.value:<14} {brief}{RESET}")

    def _help_text(self) -> str:
        lines = ["commands:"]
        if self._default is not None:
            lines.append(f"  <text>       {self._default.help or 'run the default command'}")
        for cmd in self._commands.values():
            if cmd is self._default:
                continue
            lines.append(f"  :{cmd.name:<11} {cmd.help}")
        lines += ["  :help        this help", "  :quit        exit (ctrl-d too)"]
        return "\n".join(lines)


def _repl_kwargs(cmd: Command, rest: str) -> dict[str, Any]:
    """Bind a REPL line to the command's first argument; the others default.

    The typed line goes to the first *required* positional if there is one.
    If every parameter has a default (so the argparse form is all flags),
    it still goes to the first parameter - in a REPL a line the user typed
    must reach the command, and silently returning the default instead is
    the confusing outcome.
    """
    kwargs: dict[str, Any] = {}
    filled = False
    for p in cmd.params:
        if p.default is not inspect.Parameter.empty:
            kwargs[p.name] = p.default
        elif not filled:
            kwargs[p.name] = _bind(rest, p)
            filled = True
        else:
            kwargs[p.name] = "" if _param_kind(p) is str else None
    if not filled and rest and cmd.params:
        first = cmd.params[0]
        kwargs[first.name] = _bind(rest, first)
    return kwargs


def _bind(text: str, p: inspect.Parameter) -> Any:
    return text if _param_kind(p) is str else _coerce(text, p)


def _coerce(text: str, p: inspect.Parameter) -> Any:
    try:
        return _param_kind(p)(text)
    except (TypeError, ValueError):
        return None
