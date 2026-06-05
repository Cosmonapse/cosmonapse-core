"""
cosmo validate
~~~~~~~~~~~~~~
Validate that Signals conform to the envelope spec.

Validation is STRUCTURAL ONLY. cosmo validate checks that each individual
Signal is a well-formed envelope. It never fails a trace for "wrong sequence"
or "missing FINAL". Task lifecycle and sequencing are the developer's
responsibility in their Cortex.

What it checks:
  ✓ Required fields present (v, id, trace_id, type, ts)
  ✓ id starts with 'evt_'
  ✓ trace_id starts with 'trc_'
  ✓ parent_id starts with 'evt_' (if present)
  ✓ type is a known SignalType
  ✓ ts is a valid ISO-8601 datetime
  ✓ payload is a JSON object (dict)
  ✓ meta is a JSON object (dict)

Usage:
    # Validate a .jsonl file of captured signals
    cosmo validate signals.jsonl

    # Live mode  -  validate signals on the Synapse in real time
    cosmo validate --live
    cosmo validate --live --namespace team-a

    # Strict mode  -  exit 1 on first violation
    cosmo validate signals.jsonl --strict
"""

from __future__ import annotations

import asyncio
import json
import signal
import sys
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.table import Table
from rich.text import Text

from cosmonapse import MemorySynapse, Signal, SignalType
from cosmonapse.envelope import new_event_id, new_trace_id

console = Console()


class ValidationResult:
    def __init__(self, index: int, raw: dict[str, Any]) -> None:
        self.index = index
        self.raw = raw
        self.errors: list[str] = []
        self.warnings: list[str] = []

    @property
    def valid(self) -> bool:
        return len(self.errors) == 0

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


def _validate_signal(index: int, raw: dict[str, Any]) -> ValidationResult:
    result = ValidationResult(index, raw)

    # Required fields
    for field in ("v", "id", "trace_id", "type", "ts"):
        if field not in raw:
            result.error(f"Missing required field: '{field}'")

    # Version
    if raw.get("v") != "1":
        result.error(f"Unknown protocol version: {raw.get('v')!r} (expected '1')")

    # id format
    id_val = raw.get("id", "")
    if not isinstance(id_val, str) or not id_val.startswith("evt_"):
        result.error(f"'id' must be a string starting with 'evt_', got: {id_val!r}")

    # trace_id format
    tid = raw.get("trace_id", "")
    if not isinstance(tid, str) or not tid.startswith("trc_"):
        result.error(f"'trace_id' must start with 'trc_', got: {tid!r}")

    # parent_id format (optional)
    pid = raw.get("parent_id")
    if pid is not None and (not isinstance(pid, str) or not pid.startswith("evt_")):
        result.error(f"'parent_id' must start with 'evt_' when present, got: {pid!r}")

    # type must be a known SignalType
    type_val = raw.get("type")
    known_types = {t.value for t in SignalType}
    if type_val not in known_types:
        result.error(f"Unknown signal type: {type_val!r}. Known types: {sorted(known_types)}")

    # ts must be parseable
    ts_val = raw.get("ts")
    if ts_val is not None:
        try:
            from datetime import datetime
            datetime.fromisoformat(str(ts_val).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            result.error(f"'ts' is not a valid ISO-8601 datetime: {ts_val!r}")

    # payload must be a dict
    payload = raw.get("payload", {})
    if not isinstance(payload, dict):
        result.error(f"'payload' must be a JSON object, got: {type(payload).__name__}")

    # meta must be a dict
    meta = raw.get("meta", {})
    if not isinstance(meta, dict):
        result.error(f"'meta' must be a JSON object, got: {type(meta).__name__}")

    # Warnings (non-fatal advisory checks)
    if type_val == "AGENT_OUTPUT" and not raw.get("neuron"):
        result.warn("AGENT_OUTPUT signal has no 'neuron' field  -  Cortex won't know who sent it")

    if type_val == "TASK" and "input" not in payload:
        result.warn("TASK signal payload has no 'input' field")

    if type_val in ("REGISTER", "HEARTBEAT", "DEREGISTER") and not raw.get("neuron"):
        result.warn(f"{type_val} signal should include 'neuron' field")

    return result


@click.command()
@click.argument("file", required=False, type=click.Path(exists=True, path_type=Path))
@click.option("--live", is_flag=True, help="Validate signals live on the Synapse")
@click.option("--namespace", default="default", show_default=True, help="Namespace (--live mode only)")
@click.option("--strict", is_flag=True, help="Exit 1 on first violation")
@click.option("--warnings", "show_warnings", is_flag=True, help="Show advisory warnings too")
def validate(
    file: Path | None,
    live: bool,
    namespace: str,
    strict: bool,
    show_warnings: bool,
) -> None:
    """
    Validate Signal envelopes for structural conformance.

    FILE  Path to a .jsonl file where each line is a JSON Signal.
          Omit FILE and use --live to validate the Synapse in real time.
    """
    if not file and not live:
        raise click.UsageError("Provide a FILE to validate or use --live for real-time mode.")
    if file and live:
        raise click.UsageError("Cannot use both FILE and --live at the same time.")

    if live:
        asyncio.run(_live_validate(namespace=namespace, strict=strict, show_warnings=show_warnings))
    else:
        _file_validate(file=file, strict=strict, show_warnings=show_warnings)  # type: ignore


def _file_validate(file: Path, strict: bool, show_warnings: bool) -> None:
    console.print()
    console.print(f"  [bold]cosmo validate[/bold]  [dim]{file}[/dim]")
    console.print()

    lines = [l.strip() for l in file.read_text().splitlines() if l.strip()]
    results: list[ValidationResult] = []
    total = len(lines)
    error_count = 0
    warn_count = 0

    for i, line in enumerate(lines):
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            console.print(f"  [red]Line {i+1}[/red]  JSON parse error: {exc}")
            error_count += 1
            if strict:
                sys.exit(1)
            continue

        r = _validate_signal(i + 1, raw)
        results.append(r)

        type_label = raw.get("type", "UNKNOWN")
        id_label = raw.get("id", "?")[:16]

        if r.valid and not r.warnings:
            console.print(f"  [green]✓[/green]  Line {i+1:4d}  {type_label:<18}  [dim]{id_label}[/dim]")
        elif r.valid and r.warnings:
            warn_count += len(r.warnings)
            console.print(f"  [yellow]⚠[/yellow]  Line {i+1:4d}  {type_label:<18}  [dim]{id_label}[/dim]")
            if show_warnings:
                for w in r.warnings:
                    console.print(f"             [yellow dim]warn: {w}[/yellow dim]")
        else:
            error_count += len(r.errors)
            console.print(f"  [red]✗[/red]  Line {i+1:4d}  {type_label:<18}  [dim]{id_label}[/dim]")
            for err in r.errors:
                console.print(f"             [red]error: {err}[/red]")
            if strict:
                sys.exit(1)

    console.print()
    console.print(f"  {total} signals  |  [red]{error_count} errors[/red]  |  [yellow]{warn_count} warnings[/yellow]")
    console.print()

    if error_count > 0:
        sys.exit(1)


async def _live_validate(namespace: str, strict: bool, show_warnings: bool) -> None:
    synapse = MemorySynapse()
    await synapse.connect()

    console.print()
    console.print("  [bold]cosmo validate[/bold]  [dim]--live[/dim]")
    console.print(f"  Namespace: [cyan]{namespace}[/cyan]")
    console.print("  [dim]Validating signals in real time  -  Ctrl-C to stop[/dim]")
    console.print("  " + "─" * 56)
    console.print()

    violation_count = 0

    async def handle(sig_raw_bytes_or_signal: Signal) -> None:
        nonlocal violation_count
        # In live mode we receive already-parsed Signals from the synapse
        # Re-validate by converting back to dict
        raw = json.loads(sig_raw_bytes_or_signal.model_dump_json())
        r = _validate_signal(0, raw)

        type_label = raw.get("type", "?")
        neuron = raw.get("neuron", " - ")

        if r.valid and not r.warnings:
            console.print(f"  [green]✓[/green]  {type_label:<18}  [italic]{neuron}[/italic]")
        elif r.valid and r.warnings:
            console.print(f"  [yellow]⚠[/yellow]  {type_label:<18}  [italic]{neuron}[/italic]")
            if show_warnings:
                for w in r.warnings:
                    console.print(f"       [yellow dim]warn: {w}[/yellow dim]")
        else:
            violation_count += 1
            console.print(f"  [red]✗[/red]  {type_label:<18}  [italic]{neuron}[/italic]")
            for err in r.errors:
                console.print(f"       [red]error: {err}[/red]")
            if strict:
                await synapse.close()
                sys.exit(1)

    subject = f"cosmonapse.{namespace}.>"
    # Doppler pattern: no queue_group
    await synapse.subscribe(subject, handle, queue_group=None)

    stop = asyncio.Event()

    def _on_signal(*_):
        stop.set()

    loop = asyncio.get_event_loop()
    for s in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(s, _on_signal)

    await stop.wait()
    await synapse.close()

    console.print()
    if violation_count:
        console.print(f"  [red]{violation_count} violation(s) detected.[/red]")
        sys.exit(1)
    else:
        console.print("  [green]No violations detected.[/green]")
    console.print()
