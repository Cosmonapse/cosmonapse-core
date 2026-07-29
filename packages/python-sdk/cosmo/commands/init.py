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
      receptors/       Receptor modules - each exposes RECEPTOR (unbound)
        terminal.py
      brain.py         the wiring: who hosts what + dispatch helpers
      brain.py         the only entry - `python brain.py` runs the system
      README.md

    cosmo init                  # scaffold ./cosmonapse-app
    cosmo init my-app           # scaffold ./my-app
    cosmo init my-app -n demo   # choose the namespace
    cosmo init . --force        # scaffold into the current directory

The generated project is intentionally tiny: `python brain.py` gives a
working Axon + Dendrite round-trip AND a tool call in ONE process
(in-process MemorySynapse) straight after `pip install cosmonapse`;
SYNAPSE_URL swaps the transport. It grows without restructuring: new Axon
modules go under neurons/, new tool families go under effector/, new
interfaces go under receptors/, wiring changes stay in brain.py, entries
stay thin - the README shows the 10-line worker.py to add when workers
should become their own processes.

Four primitives, four folders: Neurons think (neurons/), Engrams remember
(add engram/ when you need shared memory - see the README), Effectors act
(effector/), Receptors listen (receptors/).
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

# Unset -> brain.py runs on an in-process MemorySynapse (no broker, no
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


_RECEPTORS_INIT_PY = '''"""Receptor modules - each exposes a RECEPTOR built WITHOUT a dendrite.

A Receptor is an interface: it turns a CLI command, an HTTP request or a
chat turn into a TASK and hands the trace back as one of the three dispatch
shapes (send / wait / stream). Neurons think, Engrams remember, Effectors
act, Receptors listen.

Modules here declare *what the interface looks like*; brain.py binds them to
an orchestrator Dendrite (`RECEPTOR.bind(orch)`), the same split every other
folder follows. Building them unbound is what lets `uvicorn app:app` import
the module before there is an event loop to connect a Synapse on.

    CliReceptor    a command becomes a TASK          (core install)
    ApiReceptor    one endpoint, all three shapes    (pip install 'cosmonapse[receptor]')
    ChatReceptor   one turn, one dispatch, + voice   (pip install 'cosmonapse[receptor]')
"""
'''


_RECEPTORS_TERMINAL_PY = '''"""terminal - a CliReceptor: a typed command becomes a TASK.

A command function *returns the TASK input* - that is the whole contract.
The argparse tree and the REPL are derived from its signature:

    no default        -> positional  (a str one takes the rest of the line)
    default           -> --flag, typed from the annotation
    bool default      -> --flag (store_true)

`local=True` marks a command answered right here that never dispatches.

Built with no dendrite= on purpose; brain.py binds it. Either `neuron=` or
`capabilities=` picks the target, and both are optional here too - pass one
per call instead if a single interface fronts several Neurons.
"""
import asyncio

from cosmonapse import CliReceptor

RECEPTOR = CliReceptor(
    neuron="hello",                # addressed; or capabilities=["greet"]
    prog="__PROJECT__",
    description="Talk to the __PROJECT__ brain from a terminal.",
    timeout_s=30.0,
)


@RECEPTOR.command(help="greet someone")
def greet(name: str = "world"):
    return {"name": name}          # <- the TASK input


@RECEPTOR.command("ping", local=True, help="which neurons are registered?")
async def ping():
    """local=True: answered right here, nothing crosses the wire.

    REGISTER travels over the bus like everything else, so the registry is
    eventually consistent - in a just-started process it may still be
    filling. Poll briefly rather than reporting an empty bus. (Dispatch
    itself does not need this: an addressed TASK is filtered by the hosting
    Dendrite, so it never consults the registry.)
    """
    for _ in range(20):
        found = await RECEPTOR.dendrite.find_neurons()
        if found:
            return {"neurons": [n.neuron_id for n in found]}
        await asyncio.sleep(0.05)
    return {"neurons": []}


@RECEPTOR.on_result
def render(sig):
    """Terminal Signal -> what the terminal prints."""
    return sig.payload["output"]["message"]
'''


_BRAIN_PY = '''"""__PROJECT__ brain - the system. `python brain.py` runs it.

    python brain.py                     # interactive REPL (:ping, :help, :quit)
    python brain.py greet --name Ada    # one-shot   -> dispatch_and_wait
    python brain.py --stream greet      # one-shot   -> dispatch_and_subscribe
    python brain.py --send greet        # one-shot   -> dispatch_task

Modules under neurons/, effector/ and receptors/ declare *behaviour*
(Axons / Effectors / Receptors + hooks); this file owns *deployment* (which
Dendrite hosts what, roles, ids, and which interfaces are exposed).

There is no separate demo.py or cli.py: an interface is a component now, so
starting the brain starts its interfaces. Anything after `python brain.py`
belongs to the terminal Receptor - brain.py takes no flags of its own.

Host-side behaviour can be declared right in a module with the deferred
host decorators - no wiring here needed:

    @AXON.host.on_agent_output(neuron="hello")
    async def chain(sig): ...

    @EFFECTOR.host.on_tool_result
    async def observe(sig): ...
"""
import asyncio

from cosmonapse import (Dendrite, MemoryRegistryStore, MemorySynapse,
                        connect_synapse)

from config import NAMESPACE, SYNAPSE_URL
from effector import tools
from neurons import hello
from receptors import terminal


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


def build_edge(synapse) -> Dendrite:
    """The dispatching side, plus every interface mounted on it.

    attach_receptor is the fourth attach point - Axons think, Effectors
    act, Engrams remember, Receptors listen. Receptors are built unbound
    in receptors/ (behaviour) and mounted here (deployment), the same
    split every other folder follows.

    Mount an ApiReceptor or ChatReceptor the same way and run() serves
    them too; ones sharing a port merge onto a single app. Mount nothing
    and run() blocks as a headless worker node - still reachable with
    `cosmo dispatch`.

    registry_store: what makes find_neurons() work (the terminal `ping`).
    """
    edge = Dendrite(
        synapse=synapse, namespace=NAMESPACE,
        dendrite_id="orchestrator", heartbeat_s=0,
        registry_store=MemoryRegistryStore(),
    )
    edge.attach_receptor(terminal.RECEPTOR)
    return edge


async def main() -> int:
    """Start the whole system: the bus, both Dendrites, and the interfaces."""
    if SYNAPSE_URL:
        synapse = await connect_synapse(SYNAPSE_URL)
    else:
        synapse = MemorySynapse()      # in-process bus - no broker, no setup
        await synapse.connect()
    try:
        async with build_worker(synapse), build_edge(synapse) as edge:
            # Every mounted Receptor, concurrently. The first to finish
            # cancels the rest, so :quit in the terminal ends the process.
            return await edge.run()
    finally:
        await synapse.close()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130)
'''


_README_MD = '''# __PROJECT__

A Cosmonapse project in the standard package skeleton: one worker hosting an
Axon and an Effector, one orchestrator that dispatches a task, calls a tool,
and prints both results, and a terminal Receptor over the same brain.
Neurons think, Engrams remember, Effectors act, Receptors listen.

## Layout

```
__PROJECT__/
  config.py        shared settings (env, namespace, knobs)
  neurons/         Axon modules - each exposes AXON (or make_axon)
    hello.py
  effector/        Effector modules - each exposes EFFECTOR (Effector.serve())
    tools.py
  receptors/       Receptor modules - each exposes RECEPTOR (built unbound)
    terminal.py
  brain.py         the only entry - who hosts what, and `python brain.py`
```

Every cosmonapse-example follows this same layout, so anything you learn
there drops straight in here.

## Setup

```bash
pip install cosmonapse
```

## Run

One process, no setup - brain.py boots an in-process MemorySynapse, hosts
both sides, and runs whatever interfaces are mounted on the edge Dendrite:

```bash
python brain.py                     # the terminal Receptor's REPL
python brain.py greet --name Ada    # one-shot
```

Anything after `python brain.py` belongs to the terminal Receptor; brain.py
takes no flags of its own.

To run over a real synapse instead (same topology, different transport):

```bash
cosmo synapse start memory --namespace=__NAMESPACE__
SYNAPSE_URL=cosmo://127.0.0.1:7070 python brain.py
```

Expected output:

```
result: {'message': 'Hello, Cosmonapse!'}
tool result: {'echoed': 'Cosmonapse'}
```

## Talk to it

The same brain behind a terminal interface - no argparse, no REPL loop, no
timeout handling written by hand:

```bash
python brain.py greet --name Cosmonapse   # -> Hello, Cosmonapse!
python brain.py --stream greet            # every Signal on the trace
python brain.py --send greet              # fire-and-forget, prints the trace_id
python brain.py ping                      # local command, nothing dispatched
python brain.py                           # REPL: :ping, :help, :quit
```

Mount nothing on the edge Dendrite and `python brain.py` blocks as a
headless worker node - still reachable from another terminal:

```bash
cosmo dispatch --url=cosmo://127.0.0.1:7070 -n __NAMESPACE__ \\
    --neuron hello --input '{"name": "Ada"}'
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
  `build_worker` line from brain.py's `main()`:

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
- Serving HTTP or a chat window? Add another module under `receptors/`
  exposing `RECEPTOR`, and mount it in `build_edge` exactly like the
  terminal one. Both need `pip install 'cosmonapse[receptor]'`:

  ```python
  # receptors/http.py
  from cosmonapse import ApiReceptor
  RECEPTOR = ApiReceptor(neuron="hello", path="/run", input_key="name")

  # receptors/chat.py       - voice is the browser's Web Speech API, client side
  from cosmonapse import ChatReceptor
  RECEPTOR = ChatReceptor(neuron="hello", input_key="name", voice=True)
  ```

  ```python
  # brain.py
  edge.attach_receptor(http.RECEPTOR)     # POST /run
  edge.attach_receptor(chat.RECEPTOR)     # GET  /      (same app, same port)
  ```

  `python brain.py` then serves both on one port - HTTP Receptors sharing a
  (host, port) are merged into a single app. Give one a different `port=`
  to split them into separate servers.

  See cosmonapse-examples/17-receptors (and its RECIPES.md) for the full set.
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
    "receptors/__init__.py": _RECEPTORS_INIT_PY,
    "receptors/terminal.py": _RECEPTORS_TERMINAL_PY,
    "brain.py": _BRAIN_PY,
    "README.md": _README_MD,
}


def _render(template: str, *, namespace: str, project: str) -> str:
    return template.replace("__NAMESPACE__", namespace).replace("__PROJECT__", project)


class ScaffoldExistsError(Exception):
    """Raised by scaffold_project when the target has files and force=False."""


def scaffold_project(
    name: str, *, namespace: str = "demo", force: bool = False,
) -> tuple[Path, list[str]]:
    """Write the standard-skeleton project to ./NAME and return (target, written).

    Pure, reusable core of `cosmo init` - shared by the CLI command below and
    by the `cosmo genesis` API (cosmo/commands/_genesis.py), which needs to
    trigger the same scaffold from a browser form instead of a terminal.
    Raises ScaffoldExistsError instead of click.ClickException so callers
    that aren't Click commands (e.g. an aiohttp handler) can catch it and
    respond however fits their transport.
    """
    target = Path(name).resolve()
    project = target.name

    if target.exists() and any(target.iterdir()) and not force:
        existing = [p.name for p in _FILES_present(target)]
        if existing:
            raise ScaffoldExistsError(
                f"{target} already contains {', '.join(existing)}. "
                "Re-run with force to overwrite, or choose a new directory."
            )

    target.mkdir(parents=True, exist_ok=True)

    written: list[str] = []
    for filename, template in _FILES.items():
        dest = target / filename
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(_render(template, namespace=namespace, project=project),
                        encoding="utf-8")
        written.append(filename)

    return target, written


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
    Creates: config.py, neurons/, effector/, receptors/, brain.py, README.md
    \b
    Examples:
      cosmo init
      cosmo init my-app
      cosmo init my-app --namespace=demo
      cosmo init . --force
    """
    try:
        target, written = scaffold_project(name, namespace=namespace, force=force)
    except ScaffoldExistsError as e:
        raise click.ClickException(str(e))

    project = target.name
    click.echo(f"Scaffolded {project} in {target}")
    for filename in written:
        click.echo(f"  + {filename}")
    click.echo()
    click.echo("Next steps:")
    if target != Path.cwd():
        click.echo(f"  cd {name}")
    click.echo("  python brain.py                  # REPL - one process, no setup")
    click.echo("  python brain.py greet --name Ada  # one-shot")
    click.echo()
    click.echo("Same code over a real synapse:")
    click.echo(f"  cosmo synapse start memory --namespace={namespace}")
    click.echo("  SYNAPSE_URL=cosmo://127.0.0.1:7070 python brain.py")


def _FILES_present(target: Path) -> list[Path]:
    """Return any scaffold files that already exist in the target directory."""
    return [target / f for f in _FILES if (target / f).exists()]
