"""
cosmo init
~~~~~~~~~~
Scaffold a runnable Cosmonapse project in the **standard package skeleton**
(the layout every cosmonapse-example follows):

    my-app/
      config.py        shared settings (env, namespace, knobs)
      neurons/         Axon modules - each exposes AXON (or make_axon)
        hello.py
      engram/          Engram modules - each exposes ENGRAM
        store.py
      effector/        Effector modules - each exposes EFFECTOR (Effector.serve())
        tools.py
      receptors/       Receptor modules - each exposes RECEPTOR (unbound)
        terminal.py
      brain.py         the only entry - who hosts what, and `python brain.py`
      README.md

One of each primitive, so every folder has a worked example to copy rather
than a README paragraph to interpret.

    cosmo init                  # scaffold ./cosmonapse-app
    cosmo init my-app           # scaffold ./my-app
    cosmo init my-app -n demo   # choose the namespace
    cosmo init . --force        # scaffold into the current directory

The generated project is intentionally tiny: `python brain.py` gives a
working Axon + Dendrite round-trip AND a tool call in ONE process
(in-process MemorySynapse) straight after `pip install cosmonapse`;
SYNAPSE_URL swaps the transport. It grows without restructuring: new Axon
modules go under neurons/, new memory under engram/, new tool families
under effector/, new interfaces under receptors/, wiring changes stay in
brain.py, entries stay thin. Each node is its own Dendrite, so splitting one
into its own process is a thin entry over its builder - see the README.

Four primitives, four folders: Neurons think (neurons/), Engrams remember
(engram/), Effectors act (effector/), Receptors listen (receptors/).
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


_ENGRAM_INIT_PY = '''"""Engram modules - each exposes an ENGRAM (InMemoryEngram, SqliteEngram,
PostgresEngram, or Engram.serve())."""
'''


_ENGRAM_STORE_PY = '''"""store - an Engram: the memory Neurons RECALL from and IMPRINT to.

Two layers, because the SDK separates storage from hooks:

  * ``_backend`` is a finished backend - InMemoryEngram implements recall()
    and imprint() as real methods, so it works the moment you run it. Swap
    it for SqliteEngram or PostgresEngram (same constructor shape) when the
    memory should outlive the process.
  * ``ENGRAM`` is the served front, whose read and write surfaces are
    decorators. That is what gives this module somewhere to put behaviour -
    a cache, an ACL, a quota, a rewrite of the query - in front of the
    storage, without either layer knowing about the other.

Nothing points at this yet: it is attached in brain.py and waiting for a
Neuron to use it. To let ``hello`` read and write it, declare the dependency
on the Axon rather than importing this module there - the Axon enforces the
whitelist, so a Neuron cannot reach an Engram it was not declared against::

    from cosmonapse import EngramBinding
    AXON = Axon(..., engrams=[EngramBinding(name="store", directed_id="store")])

    async def hello(input, context, recall=None, imprint=None):
        past = await recall("store", query={"key": input.get("name")})

See cosmonapse-examples/06-engram-integration for the full round-trip.
"""
from cosmonapse import Engram, InMemoryEngram

_backend = InMemoryEngram(
    engram_id="store",
    engram_kind="keyvalue",
)

ENGRAM = Engram.serve(
    engram_id="store",
    engram_kind="keyvalue",
)


@ENGRAM.on_recall
async def recall(query, **kw):
    """Forwards to storage. Put a cache or an ACL above this line."""
    return await _backend.recall(query, **kw)


@ENGRAM.on_imprint
async def imprint(op, entry, **kw):
    return await _backend.imprint(op, entry, **kw)
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

    python brain.py                     # the brain, plus a REPL on it
    python brain.py greet --name Ada    # one-shot   -> dispatch_and_wait
    python brain.py --stream greet      # one-shot   -> dispatch_and_subscribe
    python brain.py --send greet        # one-shot   -> dispatch_task

Modules under neurons/, engram/, effector/ and receptors/ declare *behaviour*
(Axons / Engrams / Effectors / Receptors + hooks); this file owns *deployment*
(which node hosts what, roles, ids, and which interfaces are exposed).

One node, one Dendrite
----------------------
Each component gets its own Dendrite rather than sharing a "worker". A
Dendrite is a node's attachment to the synapse - its identity, its
subscriptions, its REGISTER - so one per node is what makes each of them a
participant in its own right: separately addressable, separately visible in
Prism, and movable into its own process by deleting one line here and
running that builder from its own entry. Nothing about the code below
changes when it goes distributed; only which process calls which builder.

A Dendrite can host several components, so collapsing these back into one
is always available. It just costs you the independence above.

Nothing here is "run" but the brain itself: `run_brain` starts every node,
serves every interface any of them mounts, and stops them on the way out.
There is no separate demo.py or cli.py - an interface is a component, so
starting the brain starts its interfaces. Anything after `python brain.py`
belongs to the terminal Receptor; brain.py takes no flags of its own.

The brain is not bound to those interfaces, though. `:quit` closes the REPL
and the brain keeps running, because a Receptor is one of four attachments
and not the thing the brain exists for. Ctrl-C stops it (so does Genesis's
Stop button); a one-shot command above exits with its own code.

Host-side behaviour can be declared right in a module with the deferred
host decorators - no wiring here needed:

    @AXON.host.on_agent_output(neuron="hello")
    async def chain(sig): ...

    @EFFECTOR.host.on_tool_result
    async def observe(sig): ...
"""
import asyncio
import contextlib
import signal

from cosmonapse import (Dendrite, MemoryRegistryStore, MemorySynapse,
                        connect_synapse, run_brain)

from config import NAMESPACE, SYNAPSE_URL
from effector import tools
from engram import store
from neurons import hello
from receptors import terminal


def build_hello(synapse) -> Dendrite:
    """The Neuron that thinks. role="worker": replies to TASKs, never
    dispatches - a Neuron deciding what work exists is how you get a loop."""
    node = Dendrite(
        synapse=synapse, namespace=NAMESPACE,
        dendrite_id="hello-node", role="worker",
    )
    node.attach_axon(hello.AXON)
    return node


def build_store(synapse) -> Dendrite:
    """The Engram that remembers - services RECALL and IMPRINT."""
    node = Dendrite(
        synapse=synapse, namespace=NAMESPACE,
        dendrite_id="store-node", role="worker",
    )
    node.attach_engram(store.ENGRAM)
    return node


def build_tools(synapse) -> Dendrite:
    """The Effector that acts - services TOOL_CALL, emits TOOL_RESULT."""
    node = Dendrite(
        synapse=synapse, namespace=NAMESPACE,
        dendrite_id="tools-node", role="worker",
    )
    node.attach_effector(tools.EFFECTOR)
    return node


def build_terminal(synapse) -> Dendrite:
    """The Receptor that listens, on the one node that may dispatch.

    Orchestrator role, and not by preference: attach_receptor and dispatch
    both refuse a role="worker" Dendrite, because a Receptor's whole job is
    to originate TASKs. That is why interfaces get their own node instead of
    joining one of the three above.

    registry_store is what makes find_neurons() work - the terminal `ping`.
    """
    node = Dendrite(
        synapse=synapse, namespace=NAMESPACE,
        dendrite_id="terminal-node", heartbeat_s=0,
        registry_store=MemoryRegistryStore(),
    )
    node.attach_receptor(terminal.RECEPTOR)
    return node


def _stop_on_signals() -> None:
    """Ctrl-C and SIGTERM cancel the running task instead of killing us.

    The brain is not bound to its interfaces, so a signal is the normal way
    it ends - Ctrl-C in a terminal, SIGTERM from Genesis's Stop button.
    Cancelling rather than dying is what lets run_brain unwind every node:
    DEREGISTER goes out, the Engram and Effector close, the synapse closes.

    add_signal_handler is Unix-only. On Windows, Ctrl-C still arrives as
    KeyboardInterrupt (handled below) and SIGTERM is not deliverable, so
    there is nothing to install and nothing to miss.
    """
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, AttributeError):
            loop.add_signal_handler(sig, task.cancel)


async def main() -> int:
    """Initialise the brain: the bus, then every node on it."""
    _stop_on_signals()
    if SYNAPSE_URL:
        synapse = await connect_synapse(SYNAPSE_URL)
    else:
        synapse = MemorySynapse()      # in-process bus - no broker, no setup
        await synapse.connect()
    try:
        return await run_brain(
            build_hello(synapse),
            build_store(synapse),
            build_tools(synapse),
            build_terminal(synapse),
        )
    except asyncio.CancelledError:
        return 0                       # a signal: we asked for this
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
  engram/          Engram modules - each exposes ENGRAM
    store.py
  effector/        Effector modules - each exposes EFFECTOR (Effector.serve())
    tools.py
  receptors/       Receptor modules - each exposes RECEPTOR (built unbound)
    terminal.py
  brain.py         the only entry - who hosts what, and `python brain.py`
```

One of each primitive: Neurons think, Engrams remember, Effectors act,
Receptors listen. `store.py` is attached and idle - nothing recalls from it
yet, so it is a place to grow into rather than a dependency to unpick.

Every cosmonapse-example follows this same layout, so anything you learn
there drops straight in here.

## Setup

```bash
pip install cosmonapse
```

## Run

One process, no setup - brain.py boots an in-process MemorySynapse, hosts
every node, and serves whatever interfaces any of them mount:

```bash
python brain.py                     # the terminal Receptor's REPL
python brain.py greet --name Ada    # one-shot
```

Anything after `python brain.py` belongs to the terminal Receptor; brain.py
takes no flags of its own.

`:quit` closes the REPL; the brain keeps running. Ctrl-C stops the brain
(so does Genesis's Stop button) - a Receptor is an interface onto the brain,
not the reason it is up.

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

Drop `build_terminal` from `run_brain` - or let every interface finish - and
`python brain.py` blocks as a headless node, still reachable from another
terminal:

```bash
cosmo dispatch --url=cosmo://127.0.0.1:7070 -n __NAMESPACE__ \\
    --neuron hello --input '{"name": "Ada"}'
```

## Observe the bus

When split across processes, watch every Signal cross the synapse:

```bash
cosmo prism --tail --url=cosmo://127.0.0.1:7070 --namespace=__NAMESPACE__
```

## Grow it

- New model or MCP-backed neuron? Add a module under `neurons/` exposing
  `AXON`, attach it in `brain.py` (one Dendrite can host several Axons).
- New tool family? Add a module under `effector/` exposing `EFFECTOR`
  (`Effector.serve()`, or subclass `cosmonapse.effector.base.Effector` when
  it needs its own `connect()`/`close()` lifecycle - a subprocess, an HTTP
  pool, a spawned MCP server), attach it in `brain.py` (one Dendrite can
  host several Effectors too).
- A node in its own process? Every node already has its own Dendrite, so
  this is a thin entry over that node's builder - drop it in as `hello.py`,
  run it against a real synapse, and delete `build_hello(synapse),` from
  brain.py's `run_brain(...)` call. Nothing else moves:

  ```python
  import asyncio
  from cosmonapse import connect_synapse, run_brain
  from brain import build_hello
  from config import SYNAPSE_URL

  async def main() -> int:
      synapse = await connect_synapse(SYNAPSE_URL)
      return await run_brain(build_hello(synapse))   # no interfaces: headless

  asyncio.run(main())
  ```

  That is the whole distributed story: same builders, different processes.
- Host-side behaviour (chain handlers, tool observers, persistence
  reactions) is declared right in the owning module with the deferred host
  decorators - no hand-wiring on the Dendrite instance itself:
  `@AXON.host.on_agent_output(...)`, `@EFFECTOR.host.on_tool_result(...)`,
  and (once you add an Engram) `@ENGRAM.host.on_imprint_signal(...)`.
- Serving HTTP or a chat window? Add another module under `receptors/`
  exposing `RECEPTOR`, give it a builder like `build_terminal`, and add it to
  `run_brain`. Both need `pip install 'cosmonapse[receptor]'`:

  ```python
  # receptors/http.py
  from cosmonapse import ApiReceptor
  RECEPTOR = ApiReceptor(neuron="hello", path="/run", input_key="name")

  # receptors/chat.py       - voice is the browser's Web Speech API, client side
  from cosmonapse import ChatReceptor
  RECEPTOR = ChatReceptor(neuron="hello", input_key="name", voice=True)
  ```

  ```python
  # brain.py - one node each, both handed to run_brain
  def build_http(synapse) -> Dendrite:
      node = Dendrite(synapse=synapse, namespace=NAMESPACE,
                      dendrite_id="http-node", heartbeat_s=0)
      node.attach_receptor(http.RECEPTOR)     # POST /run
      return node
  ```

  HTTP Receptors that share a `(host, port)` still merge into one app even
  when they live on different nodes - the merge is per address, not per
  Dendrite.

  `python brain.py` then serves both on one port - HTTP Receptors sharing a
  (host, port) are merged into a single app. Give one a different `port=`
  to split them into separate servers.

  See cosmonapse-examples/17-receptors (and its RECIPES.md) for the full set.
- Shared memory? `engram/store.py` is already there and attached, waiting
  for something to use it. Point a Neuron at it with an `EngramBinding` on
  the Axon (the Axon enforces the whitelist, so a Neuron cannot reach an
  Engram it was not declared against), or call `dendrite.recall` /
  `dendrite.imprint` directly - see cosmonapse-examples/06 and /15. Swap
  `InMemoryEngram` for `SqliteEngram` or `PostgresEngram`, same constructor
  shape, when the memory should outlive the process.
'''


_FILES = {
    "config.py": _CONFIG_PY,
    "neurons/__init__.py": _NEURONS_INIT_PY,
    "neurons/hello.py": _NEURONS_HELLO_PY,
    "effector/__init__.py": _EFFECTOR_INIT_PY,
    "effector/tools.py": _EFFECTOR_TOOLS_PY,
    "engram/__init__.py": _ENGRAM_INIT_PY,
    "engram/store.py": _ENGRAM_STORE_PY,
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
        existing = [p.name for p in _files_present(target)]
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
    Creates: config.py, neurons/, engram/, effector/, receptors/, brain.py,
    README.md
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
    click.echo("  python brain.py                  # REPL - one process, no setup")
    click.echo("  python brain.py greet --name Ada  # one-shot")
    click.echo()
    click.echo("Same code over a real synapse:")
    click.echo(f"  cosmo synapse start memory --namespace={namespace}")
    click.echo("  SYNAPSE_URL=cosmo://127.0.0.1:7070 python brain.py")


def _files_present(target: Path) -> list[Path]:
    """Return any scaffold files that already exist in the target directory."""
    return [target / f for f in _FILES if (target / f).exists()]
