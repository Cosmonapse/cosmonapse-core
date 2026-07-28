"""
cosmonapse._neuron_base
~~~~~~~~~~~~~~~~~~~~~~~~
Shared base class for every provider-/source-backed Neuron.

A Neuron is *anything that interacts with the real world* and exposes that
interaction behind the ``NeuronFn`` signature::

    async def __call__(input: dict, context: list) -> dict

Concrete sources live in sibling modules:

* ``neuron.py``        - LLM providers (Ollama, HuggingFace TGI / OpenAI-compat).
* ``_neuron_mcp.py``   - any stdio MCP server, spawned as a subprocess.

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
        import httpx  # type: ignore[import-not-found]
    except ImportError:
        raise ImportError(
            "httpx is required for Neuron provider wrappers.\n"
            "Install it with:  pip install httpx"
        ) from None
    else:
        return httpx  # type: ignore[no-any-return]


class _BaseNeuron:
    """Async-callable base.  Subclasses implement ``__call__``."""

    async def __call__(self, input: dict[str, Any], context: list[Any]) -> dict[str, Any]:
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Input helpers (shared by every source)
    # ------------------------------------------------------------------

    def _prompt(self, input: dict[str, Any]) -> str | None:
        """Return a plain-text prompt from common input keys, or a rendered
        continuation when the input is a clarification / permission
        follow-up TASK (re-dispatched by ``respond_to_clarification`` /
        ``respond_to_permission``)."""
        return (
            input.get("prompt")
            or input.get("text")
            or input.get("query")
            or input.get("content")
            or self._followup_prompt(input)
        )

    @staticmethod
    def _followup_prompt(input: dict[str, Any]) -> str | None:
        """Render the close-the-loop TASK shapes into a prompt continuation.

        ``respond_to_clarification`` re-dispatches
        ``{"clarification": {"question", "answer", ...}}`` and
        ``respond_to_permission`` re-dispatches
        ``{"permission": {"action", "granted", "reason"?, "ttl_ms"?, ...}}``.
        Built-in LLM Neurons have no native understanding of those keys, so
        without this rendering every default close-the-loop flow died with
        "expects 'prompt' or 'messages'". Custom neuron_fns can read the
        raw dicts directly and never hit this path.
        """
        c = input.get("clarification")
        if isinstance(c, dict):
            lines = ["You previously asked a clarifying question while working on a task."]
            if c.get("question") is not None:
                lines.append(f"Your question: {c['question']}")
            if "answer" in c:
                lines.append(f"The answer: {c['answer']}")
            extra = {k: v for k, v in c.items() if k not in ("question", "answer")}
            if extra:
                lines.append(f"Additional context: {extra}")
            lines.append("Continue the original task using this answer.")
            return "\n".join(lines)
        perm = input.get("permission")
        if isinstance(perm, dict):
            granted = perm.get("granted")
            verdict = "GRANTED" if granted else "DENIED"
            lines = ["You previously requested permission while working on a task."]
            if perm.get("action") is not None:
                lines.append(f"Requested action: {perm['action']}")
            lines.append(f"The decision: {verdict}.")
            if perm.get("reason") is not None:
                lines.append(f"Reason: {perm['reason']}")
            if perm.get("ttl_ms") is not None:
                lines.append(f"The grant is valid for {perm['ttl_ms']} ms.")
            if granted:
                lines.append("Proceed with the action and continue the original task.")
            else:
                lines.append("Do not perform the action. Continue the task another way, or explain why you cannot.")
            return "\n".join(lines)
        return None

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
        return
