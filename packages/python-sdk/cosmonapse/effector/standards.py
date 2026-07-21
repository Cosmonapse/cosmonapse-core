"""
cosmonapse.effector.standards
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Tool-call standards: recognisers for the *native* tool-call dialects
models are actually trained to emit. Teaching a hosted model a bespoke
convention invites drift; speaking its mother tongue does not. The Axon
declares which dialect its Neuron speaks (``tool_standard=``) and these
parsers translate that dialect into the one normalised shape the rest
of the protocol understands - the model never learns Cosmonapse exists.

Supported standards:

  ``hermes``   Nous/Hermes function-calling XML tags, the de-facto open
               model dialect (Qwen, Hermes, many fine-tunes)::

                   <tool_call>
                   {"name": "read", "arguments": {"path": "hello.py"}}
                   </tool_call>

  ``claude``   Anthropic tool_use content block, as JSON in text::

                   {"type": "tool_use", "id": "toolu_01...",
                    "name": "read", "input": {"path": "hello.py"}}

  ``codex``    OpenAI function-calling JSON - ``tool_calls`` array,
               legacy ``function_call``, a bare exact-keys
               ``{"name", "arguments"}`` object (or ``{"name",
               "parameters"}`` - Meta's documented Llama reply shape),
               or the Responses-API /
               schema-echo variant ``{"type": "function"|"function_call",
               "name", "arguments"|"parameters"}`` (hosted Llamas parrot
               the advertised schema wrapper - the ``type`` marker is the
               licence to accept ``parameters``); string-encoded
               ``arguments`` are decoded::

                   {"tool_calls": [{"id": "call_ab12", "type": "function",
                    "function": {"name": "read",
                                 "arguments": "{\"path\": \"hello.py\"}"}}]}

Every parser is pure and synchronous: it takes the model's text and
returns the normalised call ``{"tool": str, "args": dict, "call_id":
str | None}`` on a match, or ``None`` to fall through (so ordinary
prose and ordinary JSON output never misfire). Multiple tool calls in
one reply: the first is taken - the ONE-action-per-step contract is the
Axon's to enforce, not the parser's.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

__all__ = [
    "TOOL_STANDARDS",
    "ToolCallParser",
    "extract_tool_call",
    "parse_claude",
    "parse_codex",
    "parse_hermes",
]

#: A parser takes the model's text reply and returns the normalised
#: call ``{"tool", "args", "call_id"}`` or None.
ToolCallParser = Callable[[str], "dict[str, Any] | None"]

_HERMES_TAG = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
_FENCE_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_DECODER = json.JSONDecoder()


def _loads(text: str) -> Any:
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _first_obj(text: str) -> dict[str, Any] | None:
    """The first balanced JSON object in ``text``, tolerating trailing
    junk - models bolt comments onto their calls (``{...}}  # done``),
    which strict loads() rejects wholesale."""
    i = text.find("{")
    while i != -1:
        try:
            obj, _ = _DECODER.raw_decode(text, i)
        except ValueError:
            i = text.find("{", i + 1)
            continue
        return obj if isinstance(obj, dict) else None
    return None


def _json_candidates(text: str) -> list[dict[str, Any]]:
    """JSON objects worth inspecting, in reply order (first call wins):
    the whole reply when it *starts* with an object (trailing junk
    tolerated), then the first object inside each ``` fence (prose
    around fences is tolerated; prose-embedded bare objects are NOT
    scanned - that is where ordinary output would start misfiring)."""
    out: list[dict[str, Any]] = []
    t = text.strip()
    if t.startswith("{"):
        obj = _first_obj(t)
        if obj is not None:
            out.append(obj)
    for m in _FENCE_BLOCK.finditer(text):
        obj = _first_obj(m.group(1))
        if obj is not None:
            out.append(obj)
    return out


def _norm_args(args: Any) -> dict[str, Any] | None:
    """Normalise an arguments value: dict passes, a string-encoded JSON
    object is decoded (the codex wire shape), anything else fails."""
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        decoded = _loads(args)
        if isinstance(decoded, dict):
            return decoded
    return None


def _call(tool: Any, args: Any, call_id: Any = None) -> dict[str, Any] | None:
    if not isinstance(tool, str) or not tool:
        return None
    norm = _norm_args(args if args is not None else {})
    if norm is None:
        return None
    return {
        "tool": tool,
        "args": norm,
        "call_id": call_id if isinstance(call_id, str) and call_id else None,
    }


# ---------------------------------------------------------------------------
# The three standards
# ---------------------------------------------------------------------------


def parse_hermes(text: str) -> dict[str, Any] | None:
    """Nous/Hermes ``<tool_call>{"name", "arguments"}</tool_call>`` tags."""
    if not text or "<tool_call>" not in text:
        return None
    for m in _HERMES_TAG.finditer(text):
        obj = _loads(m.group(1))
        if isinstance(obj, dict):
            hit = _call(obj.get("name"), obj.get("arguments"), obj.get("id"))
            if hit is not None:
                return hit
    return None


def parse_claude(text: str) -> dict[str, Any] | None:
    """Anthropic ``{"type": "tool_use", "name", "input"}`` block."""
    if not text or "tool_use" not in text:
        return None
    for obj in _json_candidates(text):
        if obj.get("type") != "tool_use":
            continue
        hit = _call(obj.get("name"), obj.get("input"), obj.get("id"))
        if hit is not None:
            return hit
    return None


def parse_codex(text: str) -> dict[str, Any] | None:
    """OpenAI function-calling JSON: ``tool_calls`` array, legacy
    ``function_call``, or a bare exact-keys ``{"name", "arguments"}``."""
    if not text:
        return None
    for obj in _json_candidates(text):
        calls = obj.get("tool_calls")
        if isinstance(calls, list):
            for entry in calls:
                if not isinstance(entry, dict):
                    continue
                fn = entry.get("function")
                if not isinstance(fn, dict):
                    continue
                hit = _call(fn.get("name"), fn.get("arguments"),
                            entry.get("id"))
                if hit is not None:
                    return hit
            continue
        fc = obj.get("function_call")
        if isinstance(fc, dict):
            hit = _call(fc.get("name"), fc.get("arguments"), obj.get("id"))
            if hit is not None:
                return hit
            continue
        # Bare function-call object: exactly {"name", "arguments"} - the
        # exact-keys rule keeps ordinary JSON outputs from misfiring.
        if set(obj.keys()) == {"name", "arguments"}:
            hit = _call(obj.get("name"), obj.get("arguments"))
            if hit is not None:
                return hit
            continue
        # Meta's documented Llama JSON tool format replies with
        # {"name", "parameters"} - the args key is literally "parameters".
        # Same exact-keys guard: any extra key means it is not a call.
        if set(obj.keys()) == {"name", "parameters"}:
            hit = _call(obj.get("name"), obj.get("parameters"))
            if hit is not None:
                return hit
            continue
        # Self-marked variant: {"type": "function"|"function_call", "name",
        # "arguments"|"parameters"} - the shape Responses-API items use and
        # the one hosted models drift into by echoing the advertised schema
        # wrapper. The explicit type marker is what licenses accepting
        # "parameters" as the arguments key; without it, ordinary JSON
        # carrying a "parameters" field must never misfire. (A real schema
        # wrapper nests under a "function" key and has no top-level "name",
        # so it cannot match here.)
        if obj.get("type") in ("function", "function_call"):
            fn = obj.get("function")
            if isinstance(fn, dict):
                # Schema-echo-with-args drift: the model replays the whole
                # advertised wrapper ({"type": "function", "function":
                # {name, description, parameters-SCHEMA}}) and bolts the
                # real args on as "arguments". Only a true "arguments" key
                # matches - fn["parameters"] is the SCHEMA, never the args,
                # so a pure schema echo (no "arguments") still returns None.
                args = obj.get("arguments")
                if args is None:
                    args = fn.get("arguments")
                if args is not None:
                    hit = _call(fn.get("name"), args,
                                obj.get("id") or obj.get("call_id"))
                    if hit is not None:
                        return hit
                continue
            args = obj.get("arguments")
            if args is None:
                args = obj.get("parameters")
            hit = _call(obj.get("name"), args,
                        obj.get("id") or obj.get("call_id"))
            if hit is not None:
                return hit
    return None


TOOL_STANDARDS: dict[str, ToolCallParser] = {
    "hermes": parse_hermes,
    "claude": parse_claude,
    "codex": parse_codex,
}


def extract_tool_call(raw: Any, standard: str) -> dict[str, Any] | None:
    """Run the ``standard``'s parser over a Neuron's raw output.

    Accepts the LLM-source shape ``{"response": text}`` or a plain
    string; anything else has no text to parse. Returns the normalised
    call or None.
    """
    parser = TOOL_STANDARDS.get(standard)
    if parser is None:
        return None
    if isinstance(raw, str):
        return parser(raw)
    if isinstance(raw, dict):
        text = raw.get("response")
        if isinstance(text, str):
            return parser(text)
    return None
