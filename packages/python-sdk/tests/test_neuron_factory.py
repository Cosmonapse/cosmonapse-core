"""
Tests for the Neuron provider factory.

The provider Neurons (Ollama, HuggingFace, MCP) reach out to external systems
at call time, so we don't exercise their network paths here. We cover what can
be tested without infrastructure:

  * the factory's source dispatch and error handling,
  * the standard MCP server presets.
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


def test_http_sources_are_removed():
    # The Flask / WSGI / API HTTP-app Neuron type has been removed.
    for source in ("flask", "wsgi", "api"):
        with pytest.raises(ValueError) as exc:
            Neuron(source=source, app=object())
        assert "Unknown source" in str(exc.value)


# ---------------------------------------------------------------------------
# OpenAI / Anthropic / OpenAI-compatible aliases
# ---------------------------------------------------------------------------


def test_openai_requires_api_key(monkeypatch):
    pytest.importorskip("httpx", reason="httpx not installed")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError):
        Neuron(source="openai", model="gpt-4o")


def test_anthropic_requires_api_key(monkeypatch):
    pytest.importorskip("httpx", reason="httpx not installed")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ValueError):
        Neuron(source="anthropic", model="claude-opus-4-6")


@pytest.mark.parametrize(
    "source, endpoint",
    [
        ("groq", "https://api.groq.com/openai"),
        ("openrouter", "https://openrouter.ai/api"),
        ("together", "https://api.together.xyz"),
        ("mistral", "https://api.mistral.ai"),
    ],
)
def test_openai_compatible_aliases(source, endpoint):
    pytest.importorskip("httpx", reason="httpx not installed")
    neuron = Neuron(source=source, model="some-model")
    # Each alias is a pre-configured _HuggingFaceNeuron-compatible callable.
    assert callable(neuron)
    assert hasattr(neuron, "__call__")
    assert neuron.endpoint == endpoint
    assert neuron.use_chat_api is True


def test_anthropic_system_message_extraction(monkeypatch):
    httpx = pytest.importorskip("httpx", reason="httpx not installed")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    captured: dict = {}

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"content": [{"type": "text", "text": "ok"}]}

    async def fake_post(self, url, json=None, **kwargs):
        captured["url"] = url
        captured["body"] = json
        return _FakeResponse()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    neuron = Neuron(source="anthropic", model="claude-opus-4-6")
    result = _run(
        neuron(
            {
                "messages": [
                    {"role": "system", "content": "be terse"},
                    {"role": "user", "content": "hi"},
                ]
            },
            [],
        )
    )

    assert captured["body"]["system"] == "be terse"
    assert captured["body"]["messages"] == [{"role": "user", "content": "hi"}]
    assert result["response"] == "ok"


def test_openai_missing_model_raises_type_error():
    with pytest.raises(TypeError):
        Neuron(source="openai")  # missing required `model`
