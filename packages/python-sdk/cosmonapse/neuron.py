"""
cosmonapse.neuron
~~~~~~~~~~~~~~~~~
The Neuron factory — wrap *anything that interacts with the real world*
behind the protocol's ``NeuronFn`` signature without writing boilerplate.

A Neuron is not just an LLM agent. It is any unit of real-world behaviour:

* an **LLM / Agent**     – Ollama, HuggingFace TGI / vLLM / OpenAI-compatible,
* an **API**             – an existing Flask app or any WSGI callable,
* an **MCP server**      – any stdio MCP server, wrapped as a tool surface.

``Neuron(source=...)`` returns a callable that satisfies ``NeuronFn``
(``async (input: dict, context: list) -> dict``), so it slots directly into
``Axon.neuron_fn`` with no extra wiring — the rest of the protocol never knows
what kind of thing is behind the Neuron.

    from cosmonapse import Axon, Neuron

    # LLM
    Axon(neuron_id="chat", neuron_fn=Neuron(source="ollama", model="llama3"))

    # API (Flask / WSGI)
    Axon(neuron_id="api", neuron_fn=Neuron(source="flask", app=flask_app))

    # MCP server (stdio subprocess)
    Axon(neuron_id="files",
         neuron_fn=Neuron(source="mcp", server="filesystem", args=["/data"]))

Input convention
----------------
* LLM sources expect ``prompt`` (str) or ``messages`` (OpenAI-style list).
* The ``flask``/``wsgi`` source expects an HTTP-shaped dict
  (``method``/``path``/``json``/...) — see ``_neuron_http``.
* The ``mcp`` source expects ``tool`` + ``arguments`` — see ``_neuron_mcp``.

Output
------
LLM sources return ``{"response": "<text>", "meta": <raw payload>}``.
The ``flask`` and ``mcp`` sources return their own structured dicts (documented
in their modules) but always include a string ``response`` field so downstream
Neurons can read a result without knowing the source kind.

Soft dependencies
-----------------
* LLM sources need ``httpx`` (``pip install httpx``).
* ``flask``/``wsgi`` need ``werkzeug`` (ships with Flask).
* ``mcp`` needs the ``mcp`` package (``pip install mcp``).

None are listed in the core SDK requirements, so projects only pull in what
the Neuron sources they actually use require.
"""

from __future__ import annotations

from typing import Any

from cosmonapse._neuron_base import _BaseNeuron, _require_httpx
from cosmonapse._neuron_http import _HttpAppNeuron
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

    def _options(self) -> dict:
        opts: dict = {}
        if self.temperature is not None:
            opts["temperature"] = self.temperature
        if self.max_tokens is not None:
            opts["num_predict"] = self.max_tokens
        return opts

    async def _generate(self, prompt: str) -> dict:
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

    async def _chat(self, messages: list[dict]) -> dict:
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

    async def _generate(self, prompt: str) -> dict:
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

    async def _chat(self, messages: list[dict]) -> dict:
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
# Registry & public factory
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, type[_BaseNeuron]] = {
    # LLM / agent providers
    "ollama": _OllamaNeuron,
    "huggingface": _HuggingFaceNeuron,
    "hf": _HuggingFaceNeuron,  # convenient alias
    # API: a Flask app or any WSGI callable served in-process
    "flask": _HttpAppNeuron,
    "wsgi": _HttpAppNeuron,  # alias — any WSGI app, not just Flask
    "api": _HttpAppNeuron,   # alias
    # MCP: wrap any stdio MCP server's tools as a Neuron
    "mcp": _MCPNeuron,
}


class Neuron:
    """
    Factory for provider-backed ``NeuronFn`` callables.

    ``Neuron(...)`` returns an async-callable object that satisfies
    the ``NeuronFn`` signature::

        async def __call__(input: dict, context: list) -> dict

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

    Sources
    -------
    ``"ollama"``  *(LLM)*
        kwargs: ``model`` *(required)*, ``endpoint``, ``system``,
        ``temperature``, ``max_tokens``, ``timeout``

    ``"huggingface"`` / ``"hf"``  *(LLM)*
        kwargs: ``endpoint`` *(required)*, ``model``, ``use_chat_api``,
        ``temperature``, ``max_new_tokens``, ``api_key``, ``timeout``

    ``"flask"`` / ``"wsgi"`` / ``"api"``  *(API)*
        kwargs: ``app`` *(required)*, ``default_method``, ``default_path``,
        ``base_headers``.  Input is HTTP-shaped (``method``/``path``/``json``/
        ``data``/``query``/``headers``).  Returns
        ``{"status", "ok", "json", "response", "headers", "meta"}``.

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
