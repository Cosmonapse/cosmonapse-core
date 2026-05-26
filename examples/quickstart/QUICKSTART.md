# Cosmonapse Quickstart

From zero to a working signal pipeline — manually, step by step.

---

## Prerequisites

Install the SDK and CLI from this repo:

```bash
# SDK + bundled `cosmo` CLI (installs the `cosmo` command, including doppler)
pip install -e cosmonapse-core/packages/python-sdk

# Optional: Flask / WSGI Neuron factory + web interface
pip install -e 'cosmonapse-core/packages/python-sdk[flask]'
```

---

## Step 1 — Start a Synapse

A **Synapse** is the message bus. Every process connects to one.  
For local dev, `DevSynapseServer` is a zero-dependency TCP broker — no NATS, no Kafka.

**Option A — CLI (recommended)**

If you installed the CLI, this is one command:

```bash
cosmo synapse start memory --namespace=quickstart
```

```
  cosmo synapse start memory
  URL:        cosmo://127.0.0.1:7070
  Namespace:  quickstart
  Transport:  TCP + NDJSON  (single-host dev only)

  Connect a Dendrite with:
    await connect_synapse('cosmo://127.0.0.1:7070')

  Ctrl-C  or  cosmo synapse stop  to stop.
  ────────────────────────────────────────────────
```

The CLI also streams every Signal that crosses the bus to stdout, so you get the Doppler behaviour built-in. Pass `--quiet` if you don't want that.

**Option B — Python**

If you haven't installed the CLI, paste this into `synapse.py` and run it:

```python
import asyncio
from cosmonapse.synapse.dev import DevSynapseServer

async def main():
    server = DevSynapseServer(host="127.0.0.1", port=7070)
    await server.start()
    print(f"Synapse running at {server.url}")
    await asyncio.sleep(float("inf"))

asyncio.run(main())
```

```bash
python synapse.py
# Synapse running at cosmo://127.0.0.1:7070
```

Leave this terminal open. Everything else connects to `cosmo://127.0.0.1:7070`.

---

## Step 2 — Code a Neuron

A **Neuron** is just an async function. It has zero knowledge of the protocol — no imports from cosmonapse, no Signal boilerplate.

```python
async def hello_neuron(input: dict, context: list) -> dict:
    name = input.get("name", "world")
    return {"message": f"Hello, {name}!"}
```

That's it. The Neuron only knows about its job.

---

## Step 3 — Wrap it in an Axon

An **Axon** is the agent-side tool that gives your Neuron an identity on the bus. It validates the Neuron's output into a protocol-valid Signal, handles errors, and manages the Neuron's `neuron_id` and capabilities.

```python
from cosmonapse import Axon

axon = Axon(
    neuron_id    = "hello-neuron",   # the address other processes use to reach it
    neuron_fn    = hello_neuron,     # your function from Step 2
    capabilities = ["greet"],
    version      = "0.0.1",
)
```

The Axon doesn't run yet — it needs a Dendrite to connect it to the bus.

---

## Step 4 — Connect a Dendrite

A **Dendrite** is the synapse-side participant. It connects to the Synapse, emits `REGISTER` on behalf of your Axon, subscribes to `TASK` signals, and routes them to the right Axon.

Create `worker.py`:

```python
import asyncio
from cosmonapse import Axon, Dendrite, connect_synapse

async def hello_neuron(input: dict, context: list) -> dict:
    name = input.get("name", "world")
    return {"message": f"Hello, {name}!"}

axon = Axon(
    neuron_id    = "hello-neuron",
    neuron_fn    = hello_neuron,
    capabilities = ["greet"],
)

async def main():
    synapse = await connect_synapse("cosmo://127.0.0.1:7070")

    dendrite = Dendrite(synapse=synapse, namespace="quickstart")
    dendrite.attach_axon(axon)

    async with dendrite:
        print("Worker ready. Listening for tasks...")
        await asyncio.sleep(float("inf"))

asyncio.run(main())
```

Run it in a new terminal:

```bash
python worker.py
# Worker ready. Listening for tasks...
```

The worker is now registered on the bus. Any process can dispatch a `TASK` to `"hello-neuron"` on namespace `"quickstart"` and it will be routed here.

---

## Step 5 — Connect Doppler

Open a third terminal and run:

```bash
cosmo doppler --synapse=cosmo://127.0.0.1:7070/quickstart
```

```
● Doppler attached — namespace: quickstart
  Watching all signal types
```

Doppler is a read-only observer that streams every Signal flowing through the bus. You don't touch your code — it just taps in. Leave it running and watch signals appear in the next steps.

---

## Step 6 — Flask Server + Dendrite

Now wire an HTTP interface. The server runs an **orchestrator Dendrite** — it has no Axon, its job is to dispatch tasks and collect results.

Flask is synchronous; cosmonapse is async. The bridge: run the asyncio loop in a background thread and use `concurrent.futures.Future` to hand results back to Flask.

Create `server.py`:

```python
import asyncio
import concurrent.futures
import threading
from flask import Flask, request, jsonify
from cosmonapse import Dendrite, connect_synapse, new_trace_id

# ── asyncio loop in a background thread ──────────────────────────────────────
loop = asyncio.new_event_loop()
threading.Thread(target=loop.run_forever, daemon=True).start()

pending: dict[str, concurrent.futures.Future] = {}
orch: Dendrite = None

async def setup():
    global orch
    synapse = await connect_synapse("cosmo://127.0.0.1:7070")
    orch = Dendrite(synapse=synapse, namespace="quickstart", dendrite_id="http-orch")

    @orch.on_agent_output
    async def on_output(sig):
        fut = pending.pop(sig.trace_id, None)
        if fut and not fut.done():
            fut.set_result(sig.payload.get("output", {}))

    await orch.start()

asyncio.run_coroutine_threadsafe(setup(), loop).result(timeout=10)

# ── Flask routes ──────────────────────────────────────────────────────────────
app = Flask(__name__)

@app.post("/task")
def submit():
    data = request.get_json()
    trace_id = new_trace_id()
    fut = concurrent.futures.Future()
    pending[trace_id] = fut

    async def dispatch():
        await orch.dispatch_task(neuron="hello-neuron", input=data, trace_id=trace_id)

    asyncio.run_coroutine_threadsafe(dispatch(), loop).result(timeout=5)

    try:
        result = fut.result(timeout=10)
        return jsonify({"ok": True, "result": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

app.run(port=5000)
```

Run it in a fourth terminal:

```bash
python server.py
```

---

## Step 7 — Test with Hello

Send a task from the terminal:

```bash
curl -s -X POST http://localhost:5000/task \
     -H "Content-Type: application/json" \
     -d '{"name": "Cosmonapse"}' | python -m json.tool
```

You should get:

```json
{
  "ok": true,
  "result": {
    "message": "Hello, Cosmonapse!"
  }
}
```

And in the Doppler terminal you'll see the full signal trace:

```
REGISTER      neuron=hello-neuron  capabilities=['greet']
TASK          trace=trc_…  neuron=hello-neuron
AGENT_OUTPUT  trace=trc_…  neuron=hello-neuron  → {message: Hello, Cosmonapse!}
```

---

## What just happened

```
curl POST /task
    │
    ▼
server.py  (Flask + Orchestrator Dendrite)
    │  emits TASK Signal
    ▼
DevSynapseServer  (cosmo://127.0.0.1:7070)
    │  routes TASK to subscriber
    ▼
worker.py  (Worker Dendrite → Axon → hello_neuron)
    │  emits AGENT_OUTPUT Signal
    ▼
DevSynapseServer
    │  routes AGENT_OUTPUT back
    ▼
server.py  (on_agent_output resolves the Future)
    │
    ▼
curl receives {"message": "Hello, Cosmonapse!"}
```

Four processes, two Dendrites, one Synapse, one Neuron — connected by Signals.

---

## Next steps

**Replace the Neuron with a real LLM call** — just make `hello_neuron` async and call OpenAI/Anthropic inside it. Nothing else changes.

**Chain two Neurons** — inside `on_agent_output`, call `orch.dispatch_task(neuron="second-neuron", ...)` with the result as input.

**Go to production** — change `cosmo://127.0.0.1:7070` to `nats://localhost:4222` everywhere. The Dendrite, Axon, and Neuron code is identical.

**Persist the registry** — pass `registry_store=SqliteRegistryStore("registry.db")` to your Dendrite to track which neurons are online.
