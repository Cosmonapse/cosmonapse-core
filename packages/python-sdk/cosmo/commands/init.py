"""
cosmo init
~~~~~~~~~~
Scaffold a minimal, runnable Cosmonapse project: one worker Dendrite hosting
an Axon, and one orchestrator that dispatches a task and prints the result.

    cosmo init                  # scaffold ./cosmonapse-app
    cosmo init my-app           # scaffold ./my-app
    cosmo init my-app -n demo   # choose the namespace
    cosmo init . --force        # scaffold into the current directory

The generated project is intentionally tiny  -  two Python files plus a README
 -  so a new developer can go from `pip install cosmonapse` to a working
Axon + Dendrite round-trip in under a minute.
"""

from __future__ import annotations

from pathlib import Path

import click

# ---------------------------------------------------------------------------
# File templates. Placeholders (__NAMESPACE__, __PROJECT__) are substituted
# with str.replace so the Python f-strings / dict literals inside survive.
# ---------------------------------------------------------------------------

_WORKER_PY = '''"""
worker.py  -  a Cosmonapse worker.

Hosts one Axon (the `hello` neuron) on the synapse and waits for TASK signals.
Run the synapse first, then this worker:

    cosmo synapse start memory --namespace=__NAMESPACE__
    python worker.py
"""

import asyncio
import signal as _signal

from cosmonapse import Axon, Dendrite, connect_synapse

SYNAPSE_URL = "cosmo://127.0.0.1:7070"
NAMESPACE = "__NAMESPACE__"


# A Neuron is just an async function (input: dict, context: list) -> dict.
# It has zero knowledge of the protocol.
async def hello(input: dict, context: list) -> dict:
    name = input.get("name", "world")
    return {"message": f"Hello, {name}!"}


# An Axon gives the neuron an identity on the bus.
axon = Axon(
    neuron_id="hello",
    neuron_fn=hello,
    capabilities=["greet"],
    version="0.0.1",
)


async def main() -> None:
    synapse = await connect_synapse(SYNAPSE_URL)
    try:
        dendrite = Dendrite(
            synapse=synapse,
            namespace=NAMESPACE,
            dendrite_id="hello-worker",
        )
        dendrite.attach_axon(axon)

        async with dendrite:
            print(f"worker ready  -  neuron 'hello' on namespace {NAMESPACE!r}")
            print("Press Ctrl+C to stop.\\n")

            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (getattr(_signal, "SIGINT", None), getattr(_signal, "SIGTERM", None)):
                if sig is not None:
                    try:
                        loop.add_signal_handler(sig, stop.set)
                    except (NotImplementedError, RuntimeError):
                        pass
            try:
                await stop.wait()
            except (KeyboardInterrupt, asyncio.CancelledError):
                pass
    finally:
        await synapse.close()
        print("worker stopped.")


if __name__ == "__main__":
    asyncio.run(main())
'''


_ORCHESTRATOR_PY = '''"""
orchestrator.py  -  dispatch one task and print the result.

Run the synapse and worker first, then this orchestrator:

    cosmo synapse start memory --namespace=__NAMESPACE__   # terminal 1
    python worker.py                                       # terminal 2
    python orchestrator.py                                 # terminal 3
"""

import asyncio

from cosmonapse import Dendrite, connect_synapse, new_trace_id

SYNAPSE_URL = "cosmo://127.0.0.1:7070"
NAMESPACE = "__NAMESPACE__"


async def main() -> None:
    synapse = await connect_synapse(SYNAPSE_URL)
    try:
        result_future: asyncio.Future = asyncio.get_event_loop().create_future()
        trace_id = new_trace_id()

        orch = Dendrite(
            synapse=synapse,
            namespace=NAMESPACE,
            dendrite_id="orchestrator",
            heartbeat_s=0,
        )

        @orch.on_agent_output
        async def _on_output(sig):
            if sig.trace_id == trace_id and not result_future.done():
                result_future.set_result(sig.payload.get("output", {}))

        @orch.on_error_signal
        async def _on_error(sig):
            if sig.trace_id == trace_id and not result_future.done():
                result_future.set_exception(
                    RuntimeError(sig.payload.get("message", "error"))
                )

        async with orch:
            input_data = {"name": "Cosmonapse"}
            print(f"dispatching TASK  trace={trace_id}  neuron=hello  input={input_data}")
            await orch.dispatch_task(neuron="hello", input=input_data, trace_id=trace_id)
            result = await asyncio.wait_for(result_future, timeout=5.0)

        print(f"result: {result}")
    finally:
        await synapse.close()


if __name__ == "__main__":
    asyncio.run(main())
'''


_README_MD = '''# __PROJECT__

A minimal Cosmonapse project: one worker hosting an Axon, one orchestrator
that dispatches a task and prints the result.

## Setup

```bash
pip install cosmonapse
```

## Run

Open three terminals (or run the synapse in the background).

```bash
# 1. Start the dev synapse (the message bus)
cosmo synapse start memory --namespace=__NAMESPACE__

# 2. Start the worker
python worker.py

# 3. Dispatch a task
python orchestrator.py
```

Expected output from the orchestrator:

```
result: {'message': 'Hello, Cosmonapse!'}
```

## Observe the bus

While the worker is running, watch every Signal cross the synapse:

```bash
cosmo doppler --url=cosmo://127.0.0.1:7070 --namespace=__NAMESPACE__
```

## What's here

- `worker.py`  -  a `Neuron` (plain async fn) wrapped in an `Axon`, hosted by a
  `Dendrite` that handles REGISTER / heartbeat / TASK routing.
- `orchestrator.py`  -  a `Dendrite` that dispatches a TASK and awaits the
  AGENT_OUTPUT.
'''


_FILES = {
    "worker.py": _WORKER_PY,
    "orchestrator.py": _ORCHESTRATOR_PY,
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
    """Scaffold a minimal Axon + Dendrite project in ./NAME.

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
        existing = [p.name for p in _FILES_present(target)]
        if existing:
            raise click.ClickException(
                f"{target} already contains {', '.join(existing)}. "
                "Re-run with --force to overwrite, or choose a new directory."
            )

    target.mkdir(parents=True, exist_ok=True)

    written: list[str] = []
    for filename, template in _FILES.items():
        dest = target / filename
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
    click.echo(f"  cosmo synapse start memory --namespace={namespace}")
    click.echo("  python worker.py        # in a second terminal")
    click.echo("  python orchestrator.py  # in a third terminal")


def _FILES_present(target: Path) -> list[Path]:
    """Return any scaffold files that already exist in the target directory."""
    return [target / f for f in _FILES if (target / f).exists()]
