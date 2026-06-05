"""
examples/orchestrator_api/wsgi_app.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Raw WSGI integration  -  no framework, just the WSGI callable spec.

Useful when you want the absolute minimum layer between your server
(gunicorn, waitress, mod_wsgi) and Cosmonapse. The Dendrite is module-level,
shared by all workers in the same process.

Run (single worker, development):

    cosmo synapse start memory --namespace=api-demo   # terminal 1
    python examples/orchestrator_api/worker.py         # terminal 2
    python examples/orchestrator_api/wsgi_app.py       # terminal 3
    # or with gunicorn (sync workers):
    gunicorn --workers=1 "examples.orchestrator_api.wsgi_app:application"

    curl -X POST http://127.0.0.1:5001/ask \\
         -H "Content-Type: application/json" \\
         -d '{"prompt": "Explain WSGI in one line."}'

Note: gunicorn's sync workers run in separate processes, so each worker
process creates its own Dendrite and asyncio loop. That's fine  -  the Synapse
handles multi-producer correctly.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import threading

from cosmonapse import Dendrite, connect_synapse

SYNAPSE_URL = os.environ.get("SYNAPSE_URL", "cosmo://127.0.0.1:7070")
NAMESPACE   = "api-demo"

# ---------------------------------------------------------------------------
# Background asyncio loop (same pattern as the Flask example).
# ---------------------------------------------------------------------------
_loop: asyncio.AbstractEventLoop
_dendrite: Dendrite
_ready = threading.Event()


def _start_loop() -> None:
    global _loop, _dendrite
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    _loop.run_until_complete(_connect())
    _ready.set()
    _loop.run_forever()


async def _connect() -> None:
    global _dendrite
    synapse   = await connect_synapse(SYNAPSE_URL)
    _dendrite = Dendrite(synapse=synapse, namespace=NAMESPACE,
                         dendrite_id="wsgi-orchestrator")
    await _dendrite.__aenter__()


threading.Thread(target=_start_loop, daemon=True).start()
_ready.wait(timeout=10)   # block until connected before serving any request


# ---------------------------------------------------------------------------
# WSGI callable
# ---------------------------------------------------------------------------

def application(environ, start_response):
    method = environ["REQUEST_METHOD"]
    path   = environ.get("PATH_INFO", "/")

    if path == "/health" and method == "GET":
        body = json.dumps({"status": "ok"}).encode()
        start_response("200 OK", [
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(body))),
        ])
        return [body]

    if path == "/ask" and method == "POST":
        length  = int(environ.get("CONTENT_LENGTH") or 0)
        payload = json.loads(environ["wsgi.input"].read(length) or b"{}")
        prompt  = payload.get("prompt", "")

        if not prompt:
            body = json.dumps({"error": "prompt is required"}).encode()
            start_response("400 Bad Request", [
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        future = asyncio.run_coroutine_threadsafe(
            _dendrite.dispatch_and_wait(
                neuron="worker",
                input={"prompt": prompt},
                timeout_s=30.0,
            ),
            _loop,
        )
        try:
            reply  = future.result(timeout=32)
            output = reply.payload["output"]
            body   = json.dumps({"response": output.get("response", "")}).encode()
            start_response("200 OK", [
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(body))),
            ])
            return [body]
        except TimeoutError:
            body = json.dumps({"error": "worker timed out"}).encode()
            start_response("504 Gateway Timeout", [
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

    body = json.dumps({"error": "not found"}).encode()
    start_response("404 Not Found", [
        ("Content-Type", "application/json"),
        ("Content-Length", str(len(body))),
    ])
    return [body]


if __name__ == "__main__":
    from wsgiref.simple_server import make_server
    print("WSGI server listening on http://127.0.0.1:5001")
    with make_server("127.0.0.1", 5001, application) as httpd:
        httpd.serve_forever()
