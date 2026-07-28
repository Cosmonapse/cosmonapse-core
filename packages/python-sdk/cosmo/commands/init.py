"""
cosmo init
~~~~~~~~~~
Scaffold a runnable Cosmonapse project in the **standard package skeleton**
(the layout every cosmonapse-example follows):

    my-app/
      config.py        shared settings (env, namespace, knobs)
      neurons/         Axon modules - each exposes AXON (or make_axon)
        hello.py
      effector/        Effector modules - each exposes EFFECTOR (Effector.serve())
        tools.py
      brain.py         the wiring: who hosts what + dispatch helpers
      demo.py          entry - hosts the worker, dispatches one TASK + one tool call
      README.md

    cosmo init                  # scaffold ./cosmonapse-app
    cosmo init my-app           # scaffold ./my-app
    cosmo init my-app -n demo   # choose the namespace
    cosmo init . --force        # scaffold into the current directory

The generated project is intentionally tiny: `python demo.py` gives a
working Axon + Dendrite round-trip AND a tool call in ONE process
(in-process MemorySynapse) straight after `pip install cosmonapse`;
SYNAPSE_URL swaps the transport. It grows without restructuring: new Axon
modules go under neurons/, new tool families go under effector/, wiring
changes stay in brain.py, entries stay thin - the README shows the 10-line
worker.py to add when workers should become their own processes.

Three primitives, three folders: Neurons think (neurons/), Engrams remember
(add engram/ when you need shared memory - see the README), Effectors act
(effector/).
"""

from __future__ import annotations

from pathlib import Path

import click

# ---------------------------------------------------------------------------
# File templates. Placeholders (__NAMESPACE__, __PROJECT__) are substituted
# with str.replace so the Python f-strings / dict literals inside survive.
# ---------------------------------------------------------------------------

_CONFIG_PY = '''"""Shared settings for the __PROJECT__ package."""
import os

# Unset -> demo.py runs on an in-process MemorySynapse (no broker, no
# setup). Set it (e.g. cosmo://127.0.0.1:7070) to run over a real synapse
# instead - same topology, different transport.
SYNAPSE_URL = os.environ.get("SYNAPSE_URL", "")
NAMESPACE = "__NAMESPACE__"
'''


_NEURONS_INIT_PY = '''"""Neuron modules - each exposes an AXON (or a make_axon factory)."""
'''


_NEURONS_HELLO_PY = '''"""hello - a Neuron is just an async function, zero protocol knowledge.

Swap the body for a model call whenever you're ready - the unified factory
wraps any source behind the same NeuronFn contract:

    from cosmonapse import Neuron
    fn = Neuron(source="ollama", model="llama3")
    fn = Neuron(source="huggingface", endpoint=..., model=..., api_key=...)
    fn = Neuron(source="mcp", server="filesystem", args=["."])
"""
from cosmonapse import Axon


async def hello(input: dict, context: list) -> dict:
    name = input.get("name", "world")
    return {"message": f"Hello, {name}!"}


# The Axon gives the Neuron an identity on the bus.
AXON = Axon(
    neuron_id="hello",
    neuron_fn=hello,
    capabilities=["greet"],
    version="0.0.1",
)
'''


_EFFECTOR_INIT_PY = '''"""Effector modules - each exposes an EFFECTOR (Effector.serve(), or a
subclass of cosmonapse.effector.base.Effector for a tool family that needs
its own connect()/close() lifecycle)."""
'''


_EFFECTOR_TOOLS_PY = '''"""tools - a barebones Effector: one @EFFECTOR.on_tool_call hook, zero
protocol knowledge. Neurons think, Engrams remember, Effectors act.

Cosmonapse does not build your tools - no registries, no frameworks. A
TOOL_CALL arrives, your handler runs, its return value is emitted as the
TOOL_RESULT. Subclass the Effector ABC instead of Effector.serve() when a
tool family needs its own connect()/close() lifecycle (a subprocess, an
HTTP pool, a spawned MCP server) - see cosmonapse.effector.base.Effector.
"""
from cosmonapse import Effector

EFFECTOR = Effector.serve(
    effector_id="tools-effector",
    effector_kind="tools",
)


@EFFECTOR.on_tool_call
async def handle(tool: str, args: dict):
    if tool == "echo":
        return {"echoed": args.get("text", "")}
    return None   # unhandled -> "unhandled tool" error on TOOL_RESULT
'''


_BRAIN_PY = '''"""__PROJECT__ brain - the wiring: who hosts what.

Modules under neurons/ and effector/ declare *behaviour* (Axons/Effectors +
hooks); this file owns *deployment* (which Dendrite hosts what, roles,
ids). Entries stay thin.

Host-side behaviour can be declared right in a module with the deferred
host decorators - no wiring here needed:

    @AXON.host.on_agent_output(neuron="hello")
    async def chain(sig): ...

    @EFFECTOR.host.on_tool_result
    async def observe(sig): ...
"""
from cosmonapse import Dendrite

from config import NAMESPACE
from effector import tools
from neurons import hello


def build_worker(synapse) -> Dendrite:
    """role="worker": hosts the Axon + the Effector, replies to TASKs and
    TOOL_CALLs, cannot dispatch. One Dendrite can host several of each."""
    worker = Dendrite(
        synapse=synapse, namespace=NAMESPACE,
        dendrite_id="hello-worker", role="worker",
    )
    worker.attach_axon(hello.AXON)
    worker.attach_effector(tools.EFFECTOR)
    return worker


def build_orchestrator(synapse) -> Dendrite:
    """The dispatching side - dispatch_and_wait / Pathways live here."""
    return Dendrite(
        synapse=synapse, namespace=NAMESPACE,
        dendrite_id="orchestrator", heartbeat_s=0,
    )
'''


_DEMO_PY = '''"""demo.py - dispatch one task, call one tool, print both results.

One process, both sides: this entry hosts the worker Dendrite AND the
orchestrator. SYNAPSE_URL only swaps the transport:

    python demo.py                                   # in-process MemorySynapse
    SYNAPSE_URL=cosmo://127.0.0.1:7070 python demo.py   # a running synapse
"""
import asyncio

from cosmonapse import MemorySynapse, SignalType, connect_synapse

from brain import build_orchestrator, build_worker
from config import SYNAPSE_URL


async def main() -> None:
    if SYNAPSE_URL:
        synapse = await connect_synapse(SYNAPSE_URL)
    else:
        synapse = MemorySynapse()        # in-process bus - no broker, no setup
        await synapse.connect()
    try:
        worker = build_worker(synapse)
        orch = build_orchestrator(synapse)
        async with worker, orch:
            input_data = {"name": "Cosmonapse"}
            print(f"dispatching TASK  neuron=hello  input={input_data}")

            # dispatch_and_wait: emit the TASK, await the reply, done.
            # scope="terminal" waits for workflow conclusion - the worker
            # finalizes its output automatically (terminal-handler
            # finalize), so this resolves with a FINAL carrying the result.
            sig = await orch.dispatch_and_wait(
                neuron="hello",
                input=input_data,
                scope="terminal",
                timeout_s=5.0,
            )

            if sig.type is SignalType.ERROR:
                raise RuntimeError(sig.payload.get("message", "error"))
            print(f"result: {sig.payload.get('result', {})}")

            # Want streaming instead? The same dispatch returns a Pathway:
            #   pw = await orch.dispatch(neuron="hello", input=input_data)
            #   async for s in pw: ...

            # call_tool: emit TOOL_CALL, await TOOL_RESULT - the Effector
            # equivalent of dispatch_and_wait. No EffectorBinding/Axon
            # plumbing needed for a direct call like this.
            tool_args = {"text": "Cosmonapse"}
            print(f"calling tool  effector=tools-effector  tool=echo  args={tool_args}")
            outcome = await orch.call_tool(
                effector_id="tools-effector",
                tool="echo",
                args=tool_args,
            )
            if outcome.error:
                raise RuntimeError(outcome.error)
            print(f"tool result: {outcome.result}")
    finally:
        await synapse.close()


if __name__ == "__main__":
    asyncio.run(main())
'''


_README_MD = '''# __PROJECT__

A Cosmonapse project in the standard package skeleton: one worker hosting an
Axon and an Effector, one orchestrator that dispatches a task, calls a tool,
and prints both results. Neurons think, Engrams remember, Effectors act.

## Layout

```
__PROJECT__/
  config.py        shared settings (env, namespace, knobs)
  neurons/         Axon modules - each exposes AXON (or make_axon)
    hello.py
  effector/        Effector modules - each exposes EFFECTOR (Effector.serve())
    tools.py
  brain.py         the wiring: who hosts what + dispatch helpers
  demo.py          entry - hosts the worker, dispatches one TASK + one tool call
```

Every cosmonapse-example follows this same layout, so anything you learn
there drops straight in here.

## Setup

```bash
pip install cosmonapse
```

## Run

One process, no setup - demo.py boots an in-process MemorySynapse and hosts
both sides:

```bash
python demo.py
```

To run over a real synapse instead (same topology, different transport):

```bash
cosmo synapse start memory --namespace=__NAMESPACE__
SYNAPSE_URL=cosmo://127.0.0.1:7070 python demo.py
```

Expected output from the demo:

```
result: {'message': 'Hello, Cosmonapse!'}
tool result: {'echoed': 'Cosmonapse'}
```

## Observe the bus

When split across processes, watch every Signal cross the synapse:

```bash
cosmo doppler --url=cosmo://127.0.0.1:7070 --namespace=__NAMESPACE__
```

## Grow it

- New model or MCP-backed neuron? Add a module under `neurons/` exposing
  `AXON`, attach it in `brain.py` (one Dendrite can host several Axons).
- New tool family? Add a module under `effector/` exposing `EFFECTOR`
  (`Effector.serve()`, or subclass `cosmonapse.effector.base.Effector` when
  it needs its own `connect()`/`close()` lifecycle - a subprocess, an HTTP
  pool, a spawned MCP server), attach it in `brain.py` (one Dendrite can
  host several Effectors too).
- Workers as their own processes? That's a thin entry over `build_worker` -
  drop this in as `worker.py`, run it against a real synapse, and delete the
  `build_worker` line from demo.py:

  ```python
  import asyncio
  from cosmonapse import connect_synapse
  from brain import build_worker
  from config import SYNAPSE_URL

  async def main() -> None:
      synapse = await connect_synapse(SYNAPSE_URL)
      async with build_worker(synapse):
          await asyncio.Event().wait()      # serve until Ctrl-C

  asyncio.run(main())
  ```
- Host-side behaviour (chain handlers, tool observers, persistence
  reactions) is declared right in the owning module with the deferred host
  decorators - no hand-wiring on the Dendrite instance itself:
  `@AXON.host.on_agent_output(...)`, `@EFFECTOR.host.on_tool_result(...)`,
  and (once you add an Engram) `@ENGRAM.host.on_imprint_signal(...)`.
- Serving HTTP? Add `app.py` (FastAPI lifespan + `build_*` from brain.py) -
  see cosmonapse-examples/05 and /14 for the pattern.
- Shared memory? Add `engram/` with the backend (e.g. `InMemoryEngram`,
  `SqliteEngram`) and either an `EngramBinding` on an Axon or plain
  `dendrite.recall`/`dendrite.imprint` calls - see cosmonapse-examples/06
  and /15.
'''


_FILES = {
    "config.py": _CONFIG_PY,
    "neurons/__init__.py": _NEURONS_INIT_PY,
    "neurons/hello.py": _NEURONS_HELLO_PY,
    "effector/__init__.py": _EFFECTOR_INIT_PY,
    "effector/tools.py": _EFFECTOR_TOOLS_PY,
    "brain.py": _BRAIN_PY,
    "demo.py": _DEMO_PY,
    "README.md": _README_MD,
}


def _render(template: str, *, namespace: str, project: str) -> str:
    return template.replace("__NAMESPACE__", namespace).replace("__PROJECT__", project)


@click.command("init")
@click.argument("name", default="cosmonapse-app")
@click.option("--namespace", "-n", default="demo", show_default=True,
              help="Namespace the scaffolded project uses.")
@click.option("--force", is_flag=True, default=False,
              help="Write into the target directory even if it already "
                   "contains files (existing files with the same names are "
                   "overwritten).")
def init(name: str, namespace: str, force: bool) -> None:
    """Scaffold a standard-skeleton Cosmonapse project in ./NAME.

    \b
    Creates: config.py, neurons/, effector/, brain.py, demo.py, README.md
    \b
    Examples:
      cosmo init
      cosmo init my-app
      cosmo init my-app --namespace=demo
      cosmo init . --force
    """
    target = Path(name).resolve()
    project = target.name

    if target.exists() and any(target.iterdir()) and not force:
        existing = [p.name for p in _files_present(target)]
        if existing:
            raise click.ClickException(
                f"{target} already contains {', '.join(existing)}. "
                "Re-run with --force to overwrite, or choose a new directory."
            )

    target.mkdir(parents=True, exist_ok=True)

    written: list[str] = []
    for filename, template in _FILES.items():
        dest = target / filename
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(_render(template, namespace=namespace, project=project),
                        encoding="utf-8")
        written.append(filename)

    click.echo(f"Scaffolded {project} in {target}")
    for filename in written:
        click.echo(f"  + {filename}")
    click.echo()
    click.echo("Next steps:")
    if target != Path.cwd():
        click.echo(f"  cd {name}")
    click.echo("  python demo.py     # one process, in-process bus - no setup")
    click.echo()
    click.echo("Same code over a real synapse:")
    click.echo(f"  cosmo synapse start memory --namespace={namespace}")
    click.echo("  SYNAPSE_URL=cosmo://127.0.0.1:7070 python demo.py")


def _files_present(target: Path) -> list[Path]:
    """Return any scaffold files that already exist in the target directory."""
    return [target / f for f in _FILES if (target / f).exists()]
