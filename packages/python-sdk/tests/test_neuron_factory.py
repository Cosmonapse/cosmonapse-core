"""
Tests for the Neuron provider factory.

The provider Neurons (Ollama, HuggingFace, MCP) reach out to external systems
at call time, so we don't exercise their network paths here. We cover what can
be tested without infrastructure:

  * the factory's source dispatch and error handling,
  * the standard MCP server presets,
  * the Flask/WSGI Neuron end-to-end against a tiny in-process WSGI app
    (skipped when werkzeug isn't installed).
"""

import asyncio

import pytest

from cosmonapse import Neuron, STANDARD_MCP_SERVERS


def _run(coro):
    return asyncio.run(coro)


def test_unknown_source_raises():
    with pytest.raises(ValueError) as exc:
        Neuron(source="not-a-real-source")
    assert "Unknown source" in str(exc.value)


def test_source_is_case_insensitive_for_known_provider():
    pytest.importorskip("mcp", reason="mcp package not installed")
    # Construction doesn't spawn the server; this exercises the .lower() dispatch.
    n = Neuron(source="MCP", command="echo", args=["hi"])
    assert n is not None


def test_standard_mcp_servers_is_a_mapping():
    assert isinstance(STANDARD_MCP_SERVERS, dict)
    assert len(STANDARD_MCP_SERVERS) > 0


def test_flask_wsgi_neuron_roundtrip():
    pytest.importorskip("werkzeug", reason="werkzeug not installed (pip install 'cosmonapse[flask]')")

    def wsgi_app(environ, start_response):
        start_response("200 OK", [("Content-Type", "application/json")])
        return [b'{"greeting": "hello"}']

    neuron = Neuron(source="wsgi", app=wsgi_app)

    async def run():
        return await neuron({"method": "GET", "path": "/"}, [])

    result = _run(run())
    assert result["status"] == 200
    assert result["ok"] is True
    assert result["json"] == {"greeting": "hello"}
    assert result["meta"]["method"] == "GET"


def test_flask_source_requires_app():
    # No `app` supplied: rejected before any werkzeug import is needed.
    with pytest.raises((ValueError, TypeError)):
        Neuron(source="flask")  # missing required `app`
