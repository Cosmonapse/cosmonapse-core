"""
cosmonapse.neuron
~~~~~~~~~~~~~~~~~
The Neuron factory  -  wrap *anything that interacts with the real world*
behind the protocol's ``NeuronFn`` signature without writing boilerplate.

A Neuron is not just an LLM agent. It is any unit of real-world behaviour:

* an **LLM / Agent**     – Ollama, HuggingFace TGI / vLLM / OpenAI-compatible,
* an **MCP server**      – any stdio MCP server, wrapped as a tool surface.

``Neuron(source=...)`` returns a callable that satisfies ``NeuronFn``
(``async (input: dict[str, Any], context: list) -> dict``), so it slots directly into
``Axon.neuron_fn`` with no extra wiring  -  the rest of the protocol never knows
what kind of thing is behind the Neuron.

    from cosmonapse import Axon, Neuron

    # LLM
    Axon(neuron_id="chat", neuron_fn=Neuron(source="ollama", model="llama3"))

    # MCP server (stdio subprocess)
    Axon(neuron_id="files",
         neuron_fn=Neuron(source="mcp", server="filesystem", args=["/data"]))

Input convention
----------------
* LLM sources expect ``prompt`` (str) or ``messages`` (OpenAI-style list).
* The ``mcp`` source expects ``tool`` + ``arguments``  -  see ``_neuron_mcp``.

Output
------
LLM sources return ``{"response": "<text>", "meta": <raw payload>}``.
The ``mcp`` source returns its own structured dict (documented in its module)
but always includes a string ``response`` field so downstream Neurons can read
a result without knowing the source kind.

Soft dependencies
-----------------
* LLM sources need ``httpx`` (``pip install httpx``).
* ``mcp`` needs the ``mcp`` package (``pip install mcp``).

None are listed in the core SDK requirements, so projects only pull in what
the Neuron sources they actually use require.
"""

from __future__ import annotations

import os
import warnings
from typing import Any, Callable

from cosmonapse._neuron_base import _BaseNeuron, _require_httpx
from cosmonapse._neuron_mcp import STANDARD_MCP_SERVERS, _MCPNeuron

__all__ = ["Neuron", "STANDARD_MCP_SERVERS"]


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------

class _OllamaNeuron(_BaseNeuron):
    """
    Wraps a running `Ollama <https://ollama.com>`_ daemon.

    Parameters
    ----------
    model:
        Ollama model tag, e.g. ``"llama3"``, ``"mistral"``, ``"phi3"``.
    endpoint:
        Base URL of the Ollama daemon.  Defaults to ``http://localhost:11434``.
    system:
        Optional system prompt injected before any user message.
    temperature:
        Sampling temperature (passed as an Ollama option).
    max_tokens:
        Maximum tokens to generate (``num_predict`` in Ollama).
    timeout:
        HTTP timeout in seconds.  Defaults to 120.
    """

    def __init__(
        self,
        model: str,
        endpoint: str = "http://localhost:11434",
        system: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float = 120.0,
    ):
        self._httpx = _require_httpx()
        self.model = model
        self.endpoint = endpoint.rstrip("/")
        self.system = system
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout

    async def __call__(self, input: dict[str, Any], context: list[Any]) -> dict[str, Any]:
        prompt, messages = self._require_input(input, "Ollama")
        if messages is not None:
            return await self._chat(messages)
        return await self._generate(prompt)  # type: ignore[arg-type]

    def _options(self) -> dict[str, Any]:
        opts: dict[str, Any] = {}
        if self.temperature is not None:
            opts["temperature"] = self.temperature
        if self.max_tokens is not None:
            opts["num_predict"] = self.max_tokens
        return opts

    async def _generate(self, prompt: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
        }
        if self.system:
            body["system"] = self.system
        opts = self._options()
        if opts:
            body["options"] = opts

        async with self._httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(f"{self.endpoint}/api/generate", json=body)
            r.raise_for_status()
            data = r.json()

        return {"response": data.get("response", ""), "meta": data}

    async def _chat(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        all_messages = messages
        if self.system:
            all_messages = [{"role": "system", "content": self.system}, *messages]

        body: dict[str, Any] = {
            "model": self.model,
            "messages": all_messages,
            "stream": False,
        }
        opts = self._options()
        if opts:
            body["options"] = opts

        async with self._httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(f"{self.endpoint}/api/chat", json=body)
            r.raise_for_status()
            data = r.json()

        content = data.get("message", {}).get("content", "")
        return {"response": content, "meta": data}


# ---------------------------------------------------------------------------
# HuggingFace TGI  (also works with vLLM, LM Studio, llama.cpp server)
# ---------------------------------------------------------------------------

class _HuggingFaceNeuron(_BaseNeuron):
    """
    Wraps a `HuggingFace TGI <https://huggingface.co/docs/text-generation-inference>`_
    endpoint, or any OpenAI-compatible inference server (vLLM, LM Studio,
    llama.cpp ``--server`` mode).

    TGI auto-detection
    ------------------
    * If the input contains ``messages`` **or** ``use_chat_api=True`` is set,
      the OpenAI-compatible ``/v1/chat/completions`` path is used.
    * Otherwise the native ``/generate`` endpoint is used.

    Parameters
    ----------
    endpoint:
        Base URL of the inference server, e.g. ``http://localhost:8080``.
    model:
        Model name forwarded in the chat-completions body (required for
        multi-model servers like vLLM; ignored by single-model TGI).
    use_chat_api:
        Force the ``/v1/chat/completions`` path even for plain prompts.
    temperature:
        Sampling temperature.
    max_new_tokens:
        Maximum tokens to generate.  Defaults to 512.
    api_key:
        Bearer token – use your HF Hub token for hosted endpoints.
    timeout:
        HTTP timeout in seconds.  Defaults to 120.
    """

    def __init__(
        self,
        endpoint: str,
        model: str | None = None,
        use_chat_api: bool = False,
        temperature: float | None = None,
        max_new_tokens: int = 512,
        api_key: str | None = None,
        timeout: float = 120.0,
    ):
        self._httpx = _require_httpx()
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.use_chat_api = use_chat_api
        self.temperature = temperature
        self.max_new_tokens = max_new_tokens
        self.timeout = timeout
        self._headers: dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            self._headers["Authorization"] = f"Bearer {api_key}"

    async def __call__(self, input: dict[str, Any], context: list[Any]) -> dict[str, Any]:
        prompt, messages = self._require_input(input, "HuggingFace")

        if messages is not None or self.use_chat_api:
            msgs = messages or [{"role": "user", "content": prompt or ""}]
            return await self._chat(msgs)

        return await self._generate(prompt)  # type: ignore[arg-type]

    async def _generate(self, prompt: str) -> dict[str, Any]:
        """Native TGI /generate endpoint."""
        params: dict[str, Any] = {"max_new_tokens": self.max_new_tokens}
        if self.temperature is not None:
            params["temperature"] = self.temperature

        body = {"inputs": prompt, "parameters": params}

        async with self._httpx.AsyncClient(timeout=self.timeout, headers=self._headers) as client:
            r = await client.post(f"{self.endpoint}/generate", json=body)
            r.raise_for_status()
            data = r.json()

        # TGI returns {"generated_text": "..."} or [{"generated_text": "..."}]
        if isinstance(data, list):
            text = data[0].get("generated_text", "") if data else ""
        else:
            text = data.get("generated_text", "")

        return {"response": text, "meta": data}

    async def _chat(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """OpenAI-compatible /v1/chat/completions (TGI ≥ 1.4, vLLM, llama.cpp)."""
        body: dict[str, Any] = {
            "messages": messages,
            "max_tokens": self.max_new_tokens,
        }
        if self.model:
            body["model"] = self.model
        if self.temperature is not None:
            body["temperature"] = self.temperature

        async with self._httpx.AsyncClient(timeout=self.timeout, headers=self._headers) as client:
            r = await client.post(f"{self.endpoint}/v1/chat/completions", json=body)
            r.raise_for_status()
            data = r.json()

        content = (
            data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
        )
        return {"response": content, "meta": data}


# ---------------------------------------------------------------------------
# OpenAI  (Chat Completions API)
# ---------------------------------------------------------------------------

class _OpenAINeuron(_BaseNeuron):
    """
    Wraps the `OpenAI Chat Completions API
    <https://platform.openai.com/docs/api-reference/chat>`_.

    Uses ``httpx`` directly (no ``openai`` SDK dependency).  Point ``endpoint``
    at an Azure OpenAI deployment or a compatible proxy to reuse the same
    wrapper against other OpenAI-protocol backends.

    Parameters
    ----------
    model:
        Chat model name, e.g. ``"gpt-4o"``, ``"gpt-4o-mini"``.
    api_key:
        OpenAI API key.  If ``None``, falls back to the ``OPENAI_API_KEY``
        environment variable; a :class:`ValueError` is raised if neither is set.
    endpoint:
        API base URL.  Defaults to ``https://api.openai.com/v1``.
    temperature:
        Sampling temperature.
    max_tokens:
        Maximum tokens to generate.
    system:
        Optional system prompt injected as the first ``system`` message.
    timeout:
        HTTP timeout in seconds.  Defaults to 120.

    Input
    -----
    ``messages``
        OpenAI-style list, used as-is (the ``system`` prompt is prepended).
    ``prompt`` / ``text`` / ``query`` / ``content``
        Wrapped as ``[{"role": "user", "content": prompt}]``.

    Output
    ------
    ``{"response": "<text>", "meta": <full API response dict>}``
    """

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        endpoint: str = "https://api.openai.com/v1",
        temperature: float | None = None,
        max_tokens: int | None = None,
        system: str | None = None,
        timeout: float = 120.0,
    ):
        self._httpx = _require_httpx()
        resolved_key = api_key if api_key is not None else os.environ.get("OPENAI_API_KEY")
        if not resolved_key:
            raise ValueError(
                "OpenAI Neuron requires an API key. Pass api_key=... or set the "
                "OPENAI_API_KEY environment variable."
            )
        self.model = model
        self.api_key = resolved_key
        self.endpoint = endpoint.rstrip("/")
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.system = system
        self.timeout = timeout
        self._headers: dict[str, str] = {
            "Authorization": f"Bearer {resolved_key}",
            "Content-Type": "application/json",
        }

    async def __call__(self, input: dict[str, Any], context: list[Any]) -> dict[str, Any]:
        prompt, messages = self._require_input(input, "OpenAI")

        if messages is not None:
            msgs = list(messages)
        else:
            msgs = [{"role": "user", "content": prompt or ""}]
        if self.system:
            msgs = [{"role": "system", "content": self.system}, *msgs]

        body: dict[str, Any] = {"model": self.model, "messages": msgs}
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if self.max_tokens is not None:
            body["max_tokens"] = self.max_tokens

        async with self._httpx.AsyncClient(timeout=self.timeout, headers=self._headers) as client:
            r = await client.post(f"{self.endpoint}/chat/completions", json=body)
            r.raise_for_status()
            data = r.json()

        content = (
            data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
        )
        return {"response": content, "meta": data}


# ---------------------------------------------------------------------------
# Anthropic  (Messages API)
# ---------------------------------------------------------------------------

class _AnthropicNeuron(_BaseNeuron):
    """
    Wraps the `Anthropic Messages API
    <https://docs.anthropic.com/en/api/messages>`_ using ``httpx`` directly.

    Parameters
    ----------
    model:
        Claude model name, e.g. ``"claude-opus-4-6"``, ``"claude-sonnet-4-6"``.
    api_key:
        Anthropic API key.  If ``None``, falls back to the ``ANTHROPIC_API_KEY``
        environment variable; a :class:`ValueError` is raised if neither is set.
    system:
        Optional system prompt.  Sent as the top-level ``system`` field (the
        Anthropic API does not accept a ``system`` role inside ``messages``).
    max_tokens:
        Maximum tokens to generate.  Required by the API; defaults to 1024.
    temperature:
        Sampling temperature.
    timeout:
        HTTP timeout in seconds.  Defaults to 120.

    Input
    -----
    ``messages``
        OpenAI-style list.  Any ``{"role": "system", ...}`` entries are pulled
        out and promoted to the top-level ``system`` field (the last one wins,
        with a warning if there are several); the remaining messages pass
        through unchanged.
    ``prompt`` / ``text`` / ``query`` / ``content``
        Wrapped as ``[{"role": "user", "content": prompt}]``.

    Output
    ------
    ``{"response": "<text>", "meta": <full API response dict>}``
    """

    _ENDPOINT = "https://api.anthropic.com/v1"
    _VERSION = "2023-06-01"

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float | None = None,
        timeout: float = 120.0,
    ):
        self._httpx = _require_httpx()
        resolved_key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY")
        if not resolved_key:
            raise ValueError(
                "Anthropic Neuron requires an API key. Pass api_key=... or set the "
                "ANTHROPIC_API_KEY environment variable."
            )
        self.model = model
        self.api_key = resolved_key
        self.system = system
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout
        self._headers: dict[str, str] = {
            "anthropic-version": self._VERSION,
            "x-api-key": resolved_key,
            "content-type": "application/json",
        }

    async def __call__(self, input: dict[str, Any], context: list[Any]) -> dict[str, Any]:
        prompt, messages = self._require_input(input, "Anthropic")

        system = self.system
        if messages is not None:
            system_msgs = [m for m in messages if m.get("role") == "system"]
            if len(system_msgs) > 1:
                warnings.warn(
                    "Anthropic Neuron received multiple system messages; "
                    "using the last one.",
                    stacklevel=2,
                )
            if system_msgs:
                system = system_msgs[-1].get("content")
            msgs = [m for m in messages if m.get("role") != "system"]
        else:
            msgs = [{"role": "user", "content": prompt or ""}]

        body: dict[str, Any] = {
            "model": self.model,
            "messages": msgs,
            "max_tokens": self.max_tokens,
        }
        if system:
            body["system"] = system
        if self.temperature is not None:
            body["temperature"] = self.temperature

        async with self._httpx.AsyncClient(timeout=self.timeout, headers=self._headers) as client:
            r = await client.post(f"{self._ENDPOINT}/messages", json=body)
            r.raise_for_status()
            data = r.json()

        blocks = data.get("content", [])
        text = "".join(
            b.get("text", "") for b in blocks if b.get("type") == "text"
        )
        return {"response": text, "meta": data}


# ---------------------------------------------------------------------------
# Aliases  (OpenAI-compatible chat endpoints, pre-configured)
# ---------------------------------------------------------------------------
#
# Several hosted providers speak the OpenAI ``/v1/chat/completions`` protocol,
# so they need no new class  -  just ``_HuggingFaceNeuron`` pointed at the right
# base URL with ``use_chat_api=True``.  Each alias resolves ``api_key`` from a
# provider-specific environment variable when one isn't passed explicitly, and
# the caller can still override ``endpoint``, ``api_key`` or any other kwarg.

def _openai_compatible_alias(
    default_endpoint: str,
    api_key_env: str,
) -> Callable[..., _HuggingFaceNeuron]:
    """Build a factory that pre-configures :class:`_HuggingFaceNeuron`."""

    def _factory(
        endpoint: str | None = None,
        api_key: str | None = None,
        use_chat_api: bool = True,
        **kwargs: Any,
    ) -> _HuggingFaceNeuron:
        resolved_key = api_key if api_key is not None else os.environ.get(api_key_env)
        return _HuggingFaceNeuron(
            endpoint=endpoint or default_endpoint,
            api_key=resolved_key,
            use_chat_api=use_chat_api,
            **kwargs,
        )

    return _factory


_groq_neuron = _openai_compatible_alias("https://api.groq.com/openai", "GROQ_API_KEY")
_openrouter_neuron = _openai_compatible_alias("https://openrouter.ai/api", "OPENROUTER_API_KEY")
_together_neuron = _openai_compatible_alias("https://api.together.xyz", "TOGETHER_API_KEY")
_mistral_neuron = _openai_compatible_alias("https://api.mistral.ai", "MISTRAL_API_KEY")


# ---------------------------------------------------------------------------
# Registry & public factory
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, Callable[..., _BaseNeuron]] = {
    # LLM / agent providers
    "ollama": _OllamaNeuron,
    "huggingface": _HuggingFaceNeuron,
    "hf": _HuggingFaceNeuron,  # convenient alias
    "openai": _OpenAINeuron,
    "anthropic": _AnthropicNeuron,
    # OpenAI-compatible hosted providers (pre-configured _HuggingFaceNeuron)
    "groq": _groq_neuron,
    "openrouter": _openrouter_neuron,
    "together": _together_neuron,
    "mistral": _mistral_neuron,
    # MCP: wrap any stdio MCP server's tools as a Neuron
    "mcp": _MCPNeuron,
}


class Neuron:
    """
    Factory for provider-backed ``NeuronFn`` callables.

    ``Neuron(...)`` returns an async-callable object that satisfies
    the ``NeuronFn`` signature::

        async def __call__(input: dict[str, Any], context: list) -> dict[str, Any]

    Pass it directly to ``Axon``::

        from cosmonapse import Axon, Neuron

        # Ollama (local daemon)
        axon = Axon(
            neuron_id="chat",
            neuron_fn=Neuron(source="ollama", model="llama3"),
        )

        # HuggingFace TGI / vLLM / llama.cpp / LM Studio
        axon = Axon(
            neuron_id="summariser",
            neuron_fn=Neuron(
                source="huggingface",
                endpoint="http://localhost:8080",
            ),
        )

        # HF Inference Endpoints (hosted)
        axon = Axon(
            neuron_id="classifier",
            neuron_fn=Neuron(
                source="huggingface",
                endpoint="https://<your-endpoint>.endpoints.huggingface.cloud",
                api_key="hf_…",
                use_chat_api=True,
            ),
        )

        # OpenAI (hosted)
        axon = Axon(
            neuron_id="writer",
            neuron_fn=Neuron(source="openai", model="gpt-4o"),
        )

        # Anthropic (hosted)
        axon = Axon(
            neuron_id="reasoner",
            neuron_fn=Neuron(source="anthropic", model="claude-opus-4-6"),
        )

    Sources
    -------
    ``"ollama"``  *(LLM)*
        kwargs: ``model`` *(required)*, ``endpoint``, ``system``,
        ``temperature``, ``max_tokens``, ``timeout``

    ``"huggingface"`` / ``"hf"``  *(LLM)*
        kwargs: ``endpoint`` *(required)*, ``model``, ``use_chat_api``,
        ``temperature``, ``max_new_tokens``, ``api_key``, ``timeout``

    ``"openai"``  *(LLM)*
        kwargs: ``model`` *(required)*, ``api_key`` *(or ``OPENAI_API_KEY``)*,
        ``endpoint``, ``temperature``, ``max_tokens``, ``system``, ``timeout``

    ``"anthropic"``  *(LLM)*
        kwargs: ``model`` *(required)*, ``api_key`` *(or ``ANTHROPIC_API_KEY``)*,
        ``system``, ``max_tokens``, ``temperature``, ``timeout``

    ``"groq"`` / ``"openrouter"`` / ``"together"`` / ``"mistral"``  *(LLM)*
        OpenAI-compatible hosted endpoints  -  pre-configured
        ``"huggingface"`` Neurons with ``use_chat_api=True`` and the provider's
        base URL.  ``api_key`` falls back to ``GROQ_API_KEY`` /
        ``OPENROUTER_API_KEY`` / ``TOGETHER_API_KEY`` / ``MISTRAL_API_KEY``.
        kwargs: ``model``, ``api_key``, ``endpoint``, ``temperature``,
        ``max_new_tokens``, ``use_chat_api``, ``timeout``

    ``"mcp"``  *(MCP server)*
        kwargs: ``command`` + ``args`` **or** ``server`` (preset name) + ``args``,
        plus ``env``, ``cwd``, ``tool``.  Input is ``{"tool", "arguments"}``
        (or ``{"__list_tools__": True}``).  Returns
        ``{"response", "result", "is_error", "content", "meta"}``.
        Standard server presets are in :data:`STANDARD_MCP_SERVERS`.

    Input dict keys (LLM sources)
    -----------------------------
    ``prompt`` (str)
        Plain-text single-turn input.
    ``messages`` (list[dict])
        OpenAI-style ``[{"role": "user", "content": "…"}]`` for
        multi-turn / system-prompt workflows.

    Output (LLM sources)
    --------------------
    ``{"response": "<text>", "meta": <raw provider payload>}``
    """

    def __new__(cls, source: str, **kwargs: Any) -> _BaseNeuron:  # type: ignore[misc]
        key = source.lower()
        if key not in _REGISTRY:
            available = ", ".join(f'"{k}"' for k in _REGISTRY)
            raise ValueError(
                f"Unknown source {source!r}.  Available: {available}"
            )
        return _REGISTRY[key](**kwargs)
