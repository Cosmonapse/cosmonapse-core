# Building an Orchestrator API

**Difficulty:** Intermediate · **Primitives:** Dendrite, Synapse, Pathway, Axon

Your HTTP framework stays at the edge. A route handler receives the request,
creates a Dendrite (or reuses a long-lived one), dispatches a TASK into the
Synapse, and awaits the reply  -  then returns it to the caller. The Neurons live
in separate worker processes; the web framework never touches them directly.

```
Client → Flask / FastAPI / Express / WSGI → Dendrite.dispatch_and_wait() → Synapse → Neuron → reply
```

## Files

| File | What it shows |
|---|---|
| `worker.py` | Shared Python worker  -  one Axon, one Neuron. Start this before any framework. |
| `flask_app.py` | Flask: async route + a module-level Dendrite shared across requests. |
| `fastapi_app.py` | FastAPI: lifespan context manages Dendrite startup/teardown. |
| `wsgi_app.py` | Raw WSGI callable  -  no framework at all. |
| `express_app.ts` | Express + TypeScript  -  `dispatchTask` inside a route handler. |

All examples share the same `worker.py`; only the web layer changes.

## Setup (Python examples)

```bash
pip install cosmonapse httpx flask fastapi uvicorn
export HF_TOKEN=hf_xxxxxxxxxxxxxxxxxxxxxxxx

# terminal 1  -  start the dev synapse
cosmo synapse start memory --namespace=api-demo

# terminal 2  -  start the shared worker
python examples/orchestrator_api/worker.py

# terminal 3  -  start whichever web framework you like
python examples/orchestrator_api/flask_app.py
# or: uvicorn examples.orchestrator_api.fastapi_app:app
# or: python examples/orchestrator_api/wsgi_app.py
```

## Setup (Express example)

```bash
npm install @cosmonapse/sdk express
npm install -D tsx @types/express

# terminal 1  -  the bus (same as above)
cosmo synapse start memory --namespace=api-demo

# terminal 2  -  the worker (Python or a TS equivalent)
python examples/orchestrator_api/worker.py

# terminal 3  -  the Express server
npx tsx examples/orchestrator_api/express_app.ts
```

## Try it

```bash
curl -X POST http://127.0.0.1:5000/ask \
     -H "Content-Type: application/json" \
     -d '{"prompt": "Explain Dendrites in one sentence."}'
```

Expected response:

```json
{
  "response": "A Dendrite is the component that connects your code to the Synapse..."
}
```

## Key pattern

```python
# Reuse one Dendrite per process  -  don't create one per request.
orchestrator = Dendrite(synapse=synapse, namespace="api-demo")

@app.post("/ask")
async def ask(body: AskBody):
    reply = await orchestrator.dispatch_and_wait(
        neuron="worker",
        input={"prompt": body.prompt},
        timeout_s=30.0,
    )
    return {"response": reply.payload["output"]["response"]}
```

The Dendrite is the only Cosmonapse object your web layer needs.
It handles TASK dispatch, Pathway creation, and reply routing.
The Neuron, Axon, and Synapse are invisible to your route handlers.

## Where to go next

- [`round_robin/`](../round_robin/)  -  dispatch across a pool of workers instead of one.
- [`capability_routing/`](../capability_routing/)  -  route by capability tag instead of a fixed neuron id.
- [`no_orchestrator/`](../no_orchestrator/)  -  let the workers claim tasks without a central dispatcher.
