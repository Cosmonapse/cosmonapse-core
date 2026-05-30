"""
cosmonapse._neuron_http
~~~~~~~~~~~~~~~~~~~~~~~~
Turn an existing **Flask app** (or any WSGI callable) into a Neuron.

This is how a plain HTTP API becomes a protocol participant: the Axon hands
the TASK's ``input`` dict to the Neuron, the Neuron replays it as an in-process
HTTP request against the app, and the HTTP response becomes the Neuron output.
No socket is opened and no server is started — the request is dispatched
through the app's WSGI interface directly, so it is fast and fully isolated.

Usage
-----
::

    from flask import Flask, jsonify, request
    from cosmonapse import Axon, Neuron

    app = Flask(__name__)

    @app.post("/summarise")
    def summarise():
        body = request.get_json()
        return jsonify(summary=body["text"][:100])

    axon = Axon(
        neuron_id="summary-api",
        neuron_fn=Neuron(source="flask", app=app),
    )

Input dict
----------
Every key is optional; sensible defaults are applied.

================  ===========================================================
key               meaning
================  ===========================================================
``method``        HTTP method. Defaults to ``default_method`` (``"POST"``).
``path`` / ``url``  Request path. Defaults to ``default_path`` (``"/"``).
``json``          JSON body (dict / list). Sets ``Content-Type: application/json``.
``data``          Raw body (str / bytes / form dict) when ``json`` is absent.
``query`` / ``params``  Query-string parameters (dict).
``headers``       Extra request headers (dict).
================  ===========================================================

As a convenience, if none of ``json``/``data``/``path`` are provided but the
input *does* carry a ``prompt`` (or ``text`` / ``query`` / ``content``) key,
the whole input dict is sent as the JSON body to ``default_path`` — so an LLM
workflow can address an HTTP neuron with the same ``{"prompt": ...}`` shape it
uses for an Ollama neuron.

Output dict
-----------
::

    {
        "status": 200,                 # HTTP status code
        "ok": True,                    # status < 400
        "json": {...} | None,          # parsed body when JSON, else None
        "response": "<text>",          # decoded body as text
        "headers": {...},              # response headers
        "meta": {"method": ..., "path": ...},
    }
"""

from __future__ import annotations

import asyncio
import json as _jsonlib
from typing import Any
from urllib.parse import urlencode

from cosmonapse._neuron_base import _BaseNeuron

# Keys that control the request rather than forming the JSON body.
_CONTROL_KEYS = {"method", "path", "url", "json", "data", "query", "params", "headers"}


def _require_werkzeug():
    try:
        from werkzeug.test import Client  # noqa: F401
        return Client
    except ImportError:
        raise ImportError(
            "werkzeug is required for the Flask/WSGI Neuron wrapper.\n"
            "It ships with Flask:  pip install flask"
        ) from None


class _HttpAppNeuron(_BaseNeuron):
    """Serve a Flask app / WSGI callable in-process as a Neuron.

    Parameters
    ----------
    app:
        A Flask application instance, or any WSGI callable
        ``(environ, start_response) -> iterable``.
    default_method:
        Method used when the input dict omits ``method``. Defaults to ``POST``.
    default_path:
        Path used when the input dict omits ``path``/``url``. Defaults to ``/``.
    base_headers:
        Headers merged into every request (e.g. an auth token).
    """

    def __init__(
        self,
        app: Any,
        default_method: str = "POST",
        default_path: str = "/",
        base_headers: dict[str, str] | None = None,
    ) -> None:
        if app is None:
            raise ValueError("Neuron(source='flask', ...) requires an `app` argument.")
        self.app = app
        self.default_method = default_method.upper()
        self.default_path = default_path
        self.base_headers = dict(base_headers or {})
        self._Client = _require_werkzeug()

    # ------------------------------------------------------------------

    async def __call__(self, input: dict[str, Any], context: list[Any]) -> dict[str, Any]:
        # The WSGI dispatch is synchronous; run it off the event loop so a slow
        # view can never block the Dendrite's heartbeat / message pump.
        return await asyncio.to_thread(self._dispatch, dict(input))

    # ------------------------------------------------------------------

    def _dispatch(self, input: dict[str, Any]) -> dict[str, Any]:
        method = str(input.get("method", self.default_method)).upper()
        path = input.get("path") or input.get("url") or self.default_path

        json_body = input.get("json")
        data = input.get("data")
        query = input.get("query") or input.get("params")

        headers = dict(self.base_headers)
        headers.update(input.get("headers") or {})

        # Convenience: an LLM-style {"prompt": ...} (or text/query/content) with
        # no explicit body becomes the JSON payload, so HTTP neurons accept the
        # same input shape as provider neurons.
        if json_body is None and data is None and self._prompt(input):
            json_body = {k: v for k, v in input.items() if k not in _CONTROL_KEYS}

        if query:
            path = f"{path}?{urlencode(query, doseq=True)}"

        client = self._Client(self.app)

        open_kwargs: dict[str, Any] = {"method": method, "headers": headers}
        if json_body is not None:
            open_kwargs["json"] = json_body
        elif data is not None:
            if isinstance(data, dict):
                open_kwargs["data"] = data  # form-encoded
            else:
                open_kwargs["data"] = data

        resp = client.open(path, **open_kwargs)

        # Werkzeug >= 3 removed Response.charset. get_data(as_text=True) decodes
        # the body using the response's Content-Type charset (defaulting to
        # utf-8) and works across Werkzeug 2.x and 3.x.
        text = resp.get_data(as_text=True)

        parsed: Any = None
        ctype = resp.headers.get("Content-Type", "")
        if "application/json" in ctype and text:
            try:
                parsed = _jsonlib.loads(text)
            except ValueError:
                parsed = None

        status = resp.status_code
        return {
            "status": status,
            "ok": status < 400,
            "json": parsed,
            "response": text,
            "headers": dict(resp.headers),
            "meta": {"method": method, "path": path},
        }
