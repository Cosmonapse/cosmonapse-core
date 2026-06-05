"""
examples/orchestrator_api/fastapi_app.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
FastAPI integration: the Dendrite lives for the lifetime of the app,
managed by a lifespan context manager.

Because FastAPI is natively async, no background thread is needed  - 
route handlers can await Cosmonapse calls directly.

Run:

    cosmo synapse start memory --namespace=api-demo        # terminal 1
    python examples/orchestrator_api/worker.py              # terminal 2
    uvicorn examples.orchestrator_api.fastapi_app:app       # terminal 3
    # or:
    python examples/orchestrator_api/fastapi_app.py

    curl -X POST http://127.0.0.1:8000/ask \\
         -H "Content-Type: application/json" \\
         -d '{"prompt": "What is a Synapse?"}'
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from cosmonapse import Dendrite, connect_synapse

SYNAPSE_URL = os.environ.get("SYNAPSE_URL", "cosmo://127.0.0.1:7070")
NAMESPACE   = "api-demo"


# ---------------------------------------------------------------------------
# State holder  -  typed for IDE clarity, populated by lifespan.
# ---------------------------------------------------------------------------
class _State:
    dendrite: Dendrite


state = _State()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Connect once at startup; disconnect cleanly on shutdown."""
    synapse = await connect_synapse(SYNAPSE_URL)
    state.dendrite = Dendrite(
        synapse=synapse,
        namespace=NAMESPACE,
        dendrite_id="fastapi-orchestrator",
    )
    async with state.dendrite:
        yield
    await synapse.close()


app = FastAPI(title="Cosmonapse orchestrator API", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class AskBody(BaseModel):
    prompt: str
    timeout_s: float = 30.0


class AskResponse(BaseModel):
    response: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.post("/ask", response_model=AskResponse)
async def ask(body: AskBody):
    if not body.prompt:
        raise HTTPException(status_code=400, detail="prompt is required")
    try:
        reply = await state.dendrite.dispatch_and_wait(
            neuron="worker",
            input={"prompt": body.prompt},
            timeout_s=body.timeout_s,
        )
    except TimeoutError:
        raise HTTPException(status_code=504, detail="worker timed out")
    return AskResponse(response=reply.payload["output"].get("response", ""))


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("examples.orchestrator_api.fastapi_app:app", port=8000, reload=False)
