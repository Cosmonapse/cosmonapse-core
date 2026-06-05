"""
examples/orchestrator_api/flask_app.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Flask integration: one module-level Dendrite, shared across every request.

Creating a Dendrite per request would re-register on every call and leave
stale connections behind. The correct pattern is: connect once at startup,
reuse for the lifetime of the process.

Run:

    cosmo synapse start memory --namespace=api-demo   # terminal 1
    python examples/orchestrator_api/worker.py         # terminal 2
    python examples/orchestrator_api/flask_app.py      # terminal 3

    curl -X POST http://127.0.0.1:5000/ask \\
         -H "Content-Type: application/json" \\
         -d '{"prompt": "What is a Dendrite?"}'
"""
from __future__ import annotations

import asyncio
import os
import threading

from flask import Flask, jsonify, request

from cosmonapse import Dendrite, connect_synapse

SYNAPSE_URL = os.environ.get("SYNAPSE_URL", "cosmo://127.0.0.1:7070")
NAMESPACE   = "api-demo"

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Shared event loop + Dendrite.
#
# Flask is sync; Cosmonapse is async. We run a dedicated asyncio loop in a
# background thread and dispatch all Cosmonapse calls onto it via
# asyncio.run_coroutine_threadsafe. This avoids the overhead of creating a
# new loop per request and keeps the single Dendrite connection alive.
# ---------------------------------------------------------------------------
_loop: asyncio.AbstractEventLoop
_dendrite: Dendrite


def _start_background_loop() -> None:
    """Entry point for the background asyncio thread."""
    global _loop, _dendrite
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    _loop.run_until_complete(_connect())
    _loop.run_forever()


async def _connect() -> None:
    global _dendrite
    synapse   = await connect_synapse(SYNAPSE_URL)
    _dendrite = Dendrite(synapse=synapse, namespace=NAMESPACE,
                         dendrite_id="flask-orchestrator")
    await _dendrite.__aenter__()


# Start the background loop before the first request arrives.
_bg = threading.Thread(target=_start_background_loop, daemon=True)
_bg.start()


def _dispatch(prompt: str, timeout: float = 30.0) -> dict:
    """Blocking helper: dispatches a TASK and returns the output dict."""
    future = asyncio.run_coroutine_threadsafe(
        _dendrite.dispatch_and_wait(
            neuron="worker",
            input={"prompt": prompt},
            timeout_s=timeout,
        ),
        _loop,
    )
    reply = future.result(timeout=timeout + 2)
    return reply.payload["output"]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.post("/ask")
def ask():
    body   = request.get_json(force=True)
    prompt = body.get("prompt", "")
    if not prompt:
        return jsonify({"error": "prompt is required"}), 400
    output = _dispatch(prompt)
    return jsonify({"response": output.get("response", "")})


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    # Give the background loop a moment to connect before accepting traffic.
    import time
    time.sleep(0.5)
    app.run(port=5000, debug=False)
