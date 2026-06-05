"""
cosmonapse._neuron_base
~~~~~~~~~~~~~~~~~~~~~~~~
Shared base class for every provider-/source-backed Neuron.

A Neuron is *anything that interacts with the real world* and exposes that
interaction behind the ``NeuronFn`` signature::

    async def __call__(input: dict, context: list) -> dict

Concrete sources live in sibling modules:

* ``neuron.py``        – LLM providers (Ollama, HuggingFace TGI / OpenAI-compat).
* ``_neuron_mcp.py``   – any stdio MCP server, spawned as a subprocess.

Keeping the base here (rather than in ``neuron.py``) lets the MCP module
import it without creating an import cycle back through the public
``Neuron`` factory.
"""

from __future__ import annotations

import types
from typing import Any


def _require_httpx() -> types.ModuleType:
    """Import httpx lazily so it stays a soft dependency."""
    try:
        import httpx  # type: ignore[import-not-found]  # noqa: F401
        return httpx  # type: ignore[no-any-return]
    except ImportError:
        raise ImportError(
            "httpx is required for Neuron provider wrappers.\n"
            "Install it with:  pip install httpx"
        ) from None


class _BaseNeuron:
    """Async-callable base.  Subclasses implement ``__call__``."""

    async def __call__(self, input: dict[str, Any], context: list[Any]) -> dict[str, Any]:
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Input helpers (shared by every source)
    # ------------------------------------------------------------------

    def _prompt(self, input: dict[str, Any]) -> str | None:
        """Return a plain-text prompt from common input keys."""
        return (
            input.get("prompt")
            or input.get("text")
            or input.get("query")
            or input.get("content")
        )

    def _messages(self, input: dict[str, Any]) -> list[dict[str, Any]] | None:
        """Return OpenAI-style messages if present."""
        return input.get("messages")

    def _require_input(self, input: dict[str, Any], provider: str) -> tuple[str | None, list[dict[str, Any]] | None]:
        prompt = self._prompt(input)
        messages = self._messages(input)
        if not prompt and not messages:
            raise ValueError(
                f"{provider} Neuron expects 'prompt' or 'messages' in the input dict. "
                f"Got keys: {list(input.keys())}"
            )
        return prompt, messages

    # ------------------------------------------------------------------
    # Optional async teardown
    # ------------------------------------------------------------------

    async def aclose(self) -> None:
        """Release any resources (subprocesses, sockets, sessions).

        Sources that hold no resources (the HTTP-app and LLM wrappers when
        used per-call) can leave this as a no-op.
        """
        return None
