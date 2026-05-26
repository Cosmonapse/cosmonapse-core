"""
step3_server.py
~~~~~~~~~~~~~~~
A minimal Flask HTTP server wired to an orchestrator Dendrite.

The browser submits a task → Flask dispatches a TASK Signal → the worker's
Axon processes it → AGENT_OUTPUT comes back → Flask returns the result as JSON.

Run after synapse (step1) and worker (step2) are up:

    pip install flask
    python step3_server.py

Then open http://localhost:5000 in your browser.

Architecture
------------
Flask is synchronous; cosmonapse is async.  The bridge is simple:
  - asyncio event loop runs in a daemon background thread
  - Flask interacts via concurrent.futures.Future + asyncio.run_coroutine_threadsafe
  - pending{} maps trace_id → Future so the on_agent_output handler can
    resolve the waiting Flask request
"""

import asyncio
import concurrent.futures
import threading

from flask import Flask, jsonify, request

from cosmonapse import Dendrite, connect_synapse, new_trace_id

SYNAPSE_URL = "cosmo://127.0.0.1:7070"
NAMESPACE   = "quickstart"
FLASK_PORT  = 5000

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------
_loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
_pending: dict[str, "concurrent.futures.Future[dict]"] = {}
_orch: Dendrite | None = None


def _run_event_loop() -> None:
    asyncio.set_event_loop(_loop)
    _loop.run_forever()


threading.Thread(target=_run_event_loop, daemon=True, name="cosmo-loop").start()


# ---------------------------------------------------------------------------
# Async setup — runs once at import time inside the background loop
# ---------------------------------------------------------------------------

async def _setup_dendrite() -> None:
    global _orch
    synapse = await connect_synapse(SYNAPSE_URL)
    _orch = Dendrite(
        synapse     = synapse,
        namespace   = NAMESPACE,
        dendrite_id = "http-orch",
        heartbeat_s = 0,   # no axons here — heartbeat not needed
    )

    @_orch.on_agent_output
    async def _on_output(sig):
        fut = _pending.pop(sig.trace_id, None)
        if fut is not None and not fut.done():
            fut.set_result(sig.payload.get("output", {}))

    @_orch.on_error_signal
    async def _on_error(sig):
        fut = _pending.pop(sig.trace_id, None)
        if fut is not None and not fut.done():
            fut.set_exception(
                RuntimeError(sig.payload.get("message", "unknown error"))
            )

    await _orch.start()
    print(f"✓  Orchestrator dendrite started on namespace {NAMESPACE!r}")


# Block until dendrite is ready before Flask accepts requests
asyncio.run_coroutine_threadsafe(_setup_dendrite(), _loop).result(timeout=10)


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__)

_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Cosmonapse Quickstart</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 520px; margin: 60px auto; padding: 0 20px; }
    input { padding: 8px 12px; font-size: 1rem; width: 260px; border: 1px solid #ccc; border-radius: 6px; }
    button { padding: 8px 18px; font-size: 1rem; background: #4f46e5; color: #fff; border: none;
             border-radius: 6px; cursor: pointer; margin-left: 8px; }
    button:hover { background: #4338ca; }
    pre { background: #f3f4f6; padding: 16px; border-radius: 8px; font-size: .9rem; min-height: 48px; }
    .label { font-size: .75rem; color: #6b7280; margin-bottom: 4px; }
  </style>
</head>
<body>
  <h2>Cosmonapse — Hello Quickstart</h2>
  <p>Send a task to the <code>hello-neuron</code> and see the response.</p>
  <form id="f">
    <input id="name" type="text" placeholder="Your name" value="World" autocomplete="off" />
    <button type="submit">Send task →</button>
  </form>
  <br>
  <div class="label">Response</div>
  <pre id="out">—</pre>

  <script>
    document.getElementById("f").onsubmit = async (e) => {
      e.preventDefault();
      document.getElementById("out").textContent = "waiting…";
      try {
        const res = await fetch("/task", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: document.getElementById("name").value.trim() || "World" }),
        });
        const data = await res.json();
        document.getElementById("out").textContent = JSON.stringify(data, null, 2);
      } catch (err) {
        document.getElementById("out").textContent = "Error: " + err.message;
      }
    };
  </script>
</body>
</html>"""


@app.get("/")
def index():
    return _HTML


@app.post("/task")
def submit_task():
    data: dict = request.get_json(force=True) or {}
    trace_id = new_trace_id()

    # Thread-safe future: asyncio will resolve it, Flask will block on it
    fut: concurrent.futures.Future[dict] = concurrent.futures.Future()
    _pending[trace_id] = fut

    # Dispatch TASK Signal from within the asyncio loop
    async def _dispatch():
        assert _orch is not None
        await _orch.dispatch_task(
            neuron   = "hello-neuron",
            input    = data,
            trace_id = trace_id,
        )

    asyncio.run_coroutine_threadsafe(_dispatch(), _loop).result(timeout=5)

    # Block the Flask thread until AGENT_OUTPUT resolves the future
    try:
        result = fut.result(timeout=10)
        return jsonify({"ok": True, "trace_id": trace_id, "result": result})
    except concurrent.futures.TimeoutError:
        _pending.pop(trace_id, None)
        return jsonify({"ok": False, "error": "timeout — is the worker running?"}), 504
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


if __name__ == "__main__":
    print(f"✓  Flask server starting at http://localhost:{FLASK_PORT}")
    print("   Open that URL in your browser to send tasks.\n")
    app.run(port=FLASK_PORT, debug=False)
