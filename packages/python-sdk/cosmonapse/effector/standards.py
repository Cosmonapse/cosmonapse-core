"""
cosmonapse.effector.standards
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Tool-call recognition: how a model's request to act is recovered from
what the provider actually returned. Two layers, tried in that order.

**Native (structured).** When the Axon advertised tools through the
provider's own ``tools=`` parameter, the reply carries the call as
structured data - an OpenAI ``tool_calls`` array, an Anthropic
``tool_use`` content block, an Ollama ``message.tool_calls`` list. The
provider generated it under constrained decoding, so the JSON is
well-formed by construction. This path is dialect-independent: the
shapes are recognised by inspection, not by declaration.

**Text (scraped).** When no native channel exists - a completions-only
endpoint, a local model behind a bare ``/generate`` route, any server
that ignores ``tools=`` - the call is prose and has to be parsed out of
it. That is what the dialect parsers below do, and why ``tool_standard``
exists. This layer is a fallback, not the main road: text has no
grammar guarantee, so a call can arrive truncated, unescaped or
half-corrupted, and the parsers are built to salvage what they can
without misfiring on ordinary output.

Supported text standards:

  ``hermes``   Nous/Hermes function-calling XML tags, the de-facto open
               model dialect (Qwen, Hermes, many fine-tunes)::

                   <tool_call>
                   {"name": "read", "arguments": {"path": "hello.py"}}
                   </tool_call>

  ``claude``   Anthropic tool_use content block, as JSON in text::

                   {"type": "tool_use", "id": "toolu_01...",
                    "name": "read", "input": {"path": "hello.py"}}

  ``codex``    OpenAI function-calling JSON - ``tool_calls`` array,
               legacy ``function_call``, a bare ``{"name",
               "arguments"}`` object (or ``{"name", "parameters"}`` -
               Meta's documented Llama reply shape), or the
               Responses-API / schema-echo variant ``{"type":
               "function"|"function_call", "name",
               "arguments"|"parameters"}`` (hosted Llamas parrot the
               advertised schema wrapper - the ``type`` marker is the
               licence to accept ``parameters``); string-encoded
               ``arguments`` are decoded::

                   {"tool_calls": [{"id": "call_ab12", "type": "function",
                    "function": {"name": "read",
                                 "arguments": "{\"path\": \"hello.py\"}"}}]}

  ``auto``     Try every dialect above and take the first that matches.
               The per-dialect guards are cheap string tests, so this
               costs microseconds and survives a model that drifts
               between dialects depending on how it is served.

Every parser is pure and synchronous: it takes the model's text and
returns the normalised call ``{"tool": str, "args": dict, "call_id":
str | None}`` on a match, or ``None`` to fall through (so ordinary
prose and ordinary JSON output never misfire).

On multiple calls: the ``_all`` variants and :func:`extract_tool_calls`
return every call found, in reply order. The singular parsers and
:func:`extract_tool_call` return the first and are kept for callers
that want exactly one. Deciding how many of them may actually run is
the Axon's business, not the parser's - but the parser must not be the
place they silently disappear.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

__all__ = [
    "TOOL_STANDARDS",
    "ToolCallParser",
    "extract_native_calls",
    "extract_tool_call",
    "extract_tool_calls",
    "parse_auto",
    "parse_claude",
    "parse_codex",
    "parse_hermes",
    "tool_result_messages",
]

#: A parser takes the model's text reply and returns the normalised
#: call ``{"tool", "args", "call_id"}`` or None.
ToolCallParser = Callable[[str], "dict[str, Any] | None"]

_HERMES_TAG = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
_HERMES_OPEN = "<tool_call>"
_HERMES_CLOSE = "</tool_call>"
_FENCE_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_DECODER = json.JSONDecoder()

#: Keys a model may bolt onto an otherwise well-formed bare call without
#: it ceasing to be one. Chain-of-thought wrappers ("thoughts",
#: "reasoning") are the common case: Llama and DeepSeek prompt templates
#: encourage them, and an exact-keys guard rejects the whole call over a
#: sibling that carries no meaning for dispatch.
_IGNORABLE_KEYS = frozenset({
    "thoughts", "thought", "reasoning", "reason", "observation",
    "id", "call_id", "type", "index",
})


def _loads(text: str) -> Any:
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _scan_objs(text: str) -> list[tuple[dict[str, Any], int]]:
    """Every balanced JSON object in ``text`` with its end offset.

    Uses raw_decode rather than a regex so nesting and embedded braces
    inside strings are handled correctly, and so trailing junk after an
    object (``{...}  # done``, a corrupted closing tag) is tolerated
    instead of failing the whole parse.
    """
    out: list[tuple[dict[str, Any], int]] = []
    i = text.find("{")
    while i != -1:
        try:
            obj, end = _DECODER.raw_decode(text, i)
        except ValueError:
            i = text.find("{", i + 1)
            continue
        if isinstance(obj, dict):
            out.append((obj, end))
            i = text.find("{", max(end, i + 1))
        else:
            i = text.find("{", i + 1)
    return out


def _all_objs(text: str) -> list[dict[str, Any]]:
    """Every balanced JSON object in ``text``, in order."""
    return [o for o, _ in _scan_objs(text)]


def _trailing_obj(text: str) -> dict[str, Any] | None:
    """The object that ENDS ``text``, if the reply ends with one.

    This is the narration shape - ``I'll read that. {"name": ...}`` -
    which a fence-or-nothing rule drops on the floor, and which small
    models emit constantly. Requiring the object to be the LAST thing in
    the reply is what separates it from a model *discussing* a call
    (``The call {"name": "bash", ...} would be dangerous.``). That
    distinction is not cosmetic: scanning prose freely means a model
    explaining what a dangerous command would do ends up running it.
    """
    t = text.rstrip()
    if not t.endswith("}"):
        return None
    for obj, end in _scan_objs(t):
        if end == len(t):
            return obj
    return None


def _first_obj(text: str) -> dict[str, Any] | None:
    """The first balanced JSON object in ``text``, tolerating trailing
    junk - models bolt comments onto their calls (``{...}}  # done``),
    which strict loads() rejects wholesale."""
    objs = _all_objs(text)
    return objs[0] if objs else None


def _dedup(objs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop objects already seen. The candidate scan deliberately
    overlaps (whole reply, then fences, then bare objects in prose), so
    the same call can be found twice and must not dispatch twice."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for o in objs:
        try:
            key = json.dumps(o, sort_keys=True, default=str)
        except (TypeError, ValueError):
            key = repr(o)
        if key not in seen:
            seen.add(key)
            out.append(o)
    return out


def _json_candidates(text: str) -> list[dict[str, Any]]:
    """JSON objects worth inspecting, in reply order (first call wins):
    the whole reply when it *starts* with an object (trailing junk
    tolerated), then the first object inside each ``` fence, then a
    bare object that ENDS the reply.

    The trailing scan is last and deliberately narrow. It recovers
    ``I'll read that. {"name": "read", ...}``, a very common small-model
    shape that the fence-or-nothing rule used to drop on the floor,
    without reopening the hole it was closing: an object quoted
    mid-sentence is prose ABOUT a call, not a call, and running it would
    be the worst possible reading of the reply.
    """
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
    trailing = _trailing_obj(text)
    if trailing is not None:
        out.append(trailing)
    return _dedup(out)


def _norm_args(args: Any) -> dict[str, Any] | None:
    """Normalise an arguments value: dict passes, a string-encoded JSON
    object is decoded (the codex wire shape), anything else fails.

    An empty or whitespace-only string means no arguments: providers
    send ``"arguments": ""`` for a zero-argument tool, and reading that
    as a parse failure loses a perfectly good call."""
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        if not args.strip():
            return {}
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


def _first(calls: list[dict[str, Any]]) -> dict[str, Any] | None:
    return calls[0] if calls else None


# ---------------------------------------------------------------------------
# Native (structured) recognition
# ---------------------------------------------------------------------------
# Read the provider's own tool-call channel out of the raw response the
# Neuron stashed on ``meta``. Recognised by shape, so one function covers
# every OpenAI-compatible server without being told which one it is.


def _native_openai(meta: dict[str, Any]) -> list[dict[str, Any]]:
    """``choices[0].message.tool_calls`` - OpenAI, vLLM, TGI, together,
    groq, openrouter, mistral, Azure."""
    choices = meta.get("choices")
    if not isinstance(choices, list) or not choices:
        return []
    first = choices[0]
    if not isinstance(first, dict):
        return []
    msg = first.get("message")
    if not isinstance(msg, dict):
        return []
    return _openai_entries(msg.get("tool_calls"))


def _openai_entries(entries: Any) -> list[dict[str, Any]]:
    if not isinstance(entries, list):
        return []
    out: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        fn = entry.get("function")
        if not isinstance(fn, dict):
            continue
        hit = _call(fn.get("name"), fn.get("arguments"), entry.get("id"))
        if hit is not None:
            out.append(hit)
    return out


def _native_anthropic(meta: dict[str, Any]) -> list[dict[str, Any]]:
    """``content[]`` blocks with ``type == "tool_use"``.

    This is the block ``_AnthropicNeuron`` used to filter away while
    keeping only the text, which made the whole claude dialect
    unreachable against the real API."""
    blocks = meta.get("content")
    if not isinstance(blocks, list):
        return []
    out: list[dict[str, Any]] = []
    for b in blocks:
        if not isinstance(b, dict) or b.get("type") != "tool_use":
            continue
        hit = _call(b.get("name"), b.get("input"), b.get("id"))
        if hit is not None:
            out.append(hit)
    return out


def _native_ollama(meta: dict[str, Any]) -> list[dict[str, Any]]:
    """``message.tool_calls`` - Ollama's chat route. Arguments arrive as
    a real object and there is no call id."""
    msg = meta.get("message")
    if not isinstance(msg, dict):
        return []
    return _openai_entries(msg.get("tool_calls"))


def extract_native_calls(raw: Any) -> list[dict[str, Any]]:
    """Structured tool calls from a Neuron's raw output, or ``[]``.

    Looks at ``raw["meta"]`` (where every provider wrapper stashes the
    unmodified API response) and falls back to treating ``raw`` itself
    as the response, so a caller holding only the provider payload can
    use this directly. Tries every known shape: a response carries at
    most one of them, so there is nothing to disambiguate.
    """
    if not isinstance(raw, dict):
        return []
    meta = raw.get("meta")
    if not isinstance(meta, dict):
        meta = raw
    for reader in (_native_openai, _native_anthropic, _native_ollama):
        hits = reader(meta)
        if hits:
            return hits
    return []


# ---------------------------------------------------------------------------
# The text standards
# ---------------------------------------------------------------------------


def parse_hermes_all(text: str) -> list[dict[str, Any]]:
    """Every Nous/Hermes ``<tool_call>`` call in ``text``.

    Well-formed tagged calls are taken first. If none parse but an
    opening or closing tag is present, the call is recovered by decoding
    from just past the opening tag: a reply truncated at ``max_tokens``
    loses its closing tag, and served endpoints have been observed
    corrupting either tag into unrelated tokens. Both used to degrade
    silently into "this is the final answer", which is the worst
    available failure - the agent stops mid-task and reports success.
    """
    if not text or (_HERMES_OPEN not in text and _HERMES_CLOSE not in text):
        return []
    out: list[dict[str, Any]] = []
    for m in _HERMES_TAG.finditer(text):
        obj = _loads(m.group(1))
        if isinstance(obj, dict):
            hit = _call(obj.get("name"), obj.get("arguments"), obj.get("id"))
            if hit is not None:
                out.append(hit)
    if out:
        return out
    # Salvage path: unclosed or corrupted tags.
    start = text.find(_HERMES_OPEN)
    tail = text[start + len(_HERMES_OPEN):] if start != -1 else text
    for obj in _all_objs(tail):
        hit = _call(obj.get("name"), obj.get("arguments"), obj.get("id"))
        if hit is not None:
            out.append(hit)
            break
    return out


def parse_hermes(text: str) -> dict[str, Any] | None:
    """Nous/Hermes ``<tool_call>{"name", "arguments"}</tool_call>`` tags."""
    return _first(parse_hermes_all(text))


def parse_claude_all(text: str) -> list[dict[str, Any]]:
    """Every Anthropic ``{"type": "tool_use", ...}`` object in ``text``."""
    if not text or "tool_use" not in text:
        return []
    out: list[dict[str, Any]] = []
    for obj in _json_candidates(text):
        if obj.get("type") != "tool_use":
            continue
        hit = _call(obj.get("name"), obj.get("input"), obj.get("id"))
        if hit is not None:
            out.append(hit)
    return out


def parse_claude(text: str) -> dict[str, Any] | None:
    """Anthropic ``{"type": "tool_use", "name", "input"}`` block."""
    return _first(parse_claude_all(text))


def _keys_match(obj: dict[str, Any], *names: str) -> bool:
    """Does ``obj`` carry exactly ``names``, ignoring meaningless
    siblings? The exact-keys rule is what keeps ordinary JSON output
    from misfiring as a call; ``_IGNORABLE_KEYS`` widens it just far
    enough to survive a chain-of-thought wrapper."""
    return set(obj.keys()) - _IGNORABLE_KEYS == set(names)


def parse_codex_all(text: str) -> list[dict[str, Any]]:
    """Every OpenAI-style function call in ``text``.

    A ``tool_calls`` array yields all of its entries - that is where
    parallel calls live, and dropping the tail there is how a model's
    second and third action vanish without a trace.
    """
    if not text:
        return []
    out: list[dict[str, Any]] = []
    for obj in _json_candidates(text):
        calls = obj.get("tool_calls")
        if isinstance(calls, list):
            out.extend(_openai_entries(calls))
            continue
        fc = obj.get("function_call")
        if isinstance(fc, dict):
            hit = _call(fc.get("name"), fc.get("arguments"), obj.get("id"))
            if hit is not None:
                out.append(hit)
            continue
        # Bare function-call object: {"name", "arguments"} - the
        # exact-keys rule keeps ordinary JSON outputs from misfiring.
        if _keys_match(obj, "name", "arguments"):
            hit = _call(obj.get("name"), obj.get("arguments"),
                        obj.get("id") or obj.get("call_id"))
            if hit is not None:
                out.append(hit)
            continue
        # Meta's documented Llama JSON tool format replies with
        # {"name", "parameters"} - the args key is literally "parameters".
        if _keys_match(obj, "name", "parameters"):
            hit = _call(obj.get("name"), obj.get("parameters"),
                        obj.get("id") or obj.get("call_id"))
            if hit is not None:
                out.append(hit)
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
                        out.append(hit)
                continue
            args = obj.get("arguments")
            if args is None:
                args = obj.get("parameters")
            hit = _call(obj.get("name"), args,
                        obj.get("id") or obj.get("call_id"))
            if hit is not None:
                out.append(hit)
    return out


def parse_codex(text: str) -> dict[str, Any] | None:
    """OpenAI function-calling JSON: ``tool_calls`` array, legacy
    ``function_call``, or a bare exact-keys ``{"name", "arguments"}``."""
    return _first(parse_codex_all(text))


def parse_auto_all(text: str) -> list[dict[str, Any]]:
    """Try every dialect, take the first that matches.

    Order is by specificity of the guard, not by popularity: hermes
    needs a literal tag, claude needs the ``tool_use`` marker, and codex
    is last because its bare-object shape is the loosest of the three.
    """
    for parser in (parse_hermes_all, parse_claude_all, parse_codex_all):
        hits = parser(text)
        if hits:
            return hits
    return []


def parse_auto(text: str) -> dict[str, Any] | None:
    """First tool call in ``text``, whichever dialect it is written in."""
    return _first(parse_auto_all(text))


TOOL_STANDARDS: dict[str, ToolCallParser] = {
    "hermes": parse_hermes,
    "claude": parse_claude,
    "codex": parse_codex,
    "auto": parse_auto,
}

#: The list-returning counterpart of TOOL_STANDARDS, used by
#: extract_tool_calls. Kept separate so TOOL_STANDARDS keeps its
#: published single-call contract.
_ALL_PARSERS: dict[str, Callable[[str], list[dict[str, Any]]]] = {
    "hermes": parse_hermes_all,
    "claude": parse_claude_all,
    "codex": parse_codex_all,
    "auto": parse_auto_all,
}


def _text_of(raw: Any) -> str | None:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        text = raw.get("response")
        if isinstance(text, str):
            return text
    return None


def extract_tool_calls(
    raw: Any,
    standard: str | None,
    *,
    native: bool = True,
) -> list[dict[str, Any]]:
    """Every tool call in a Neuron's raw output, in reply order.

    Native structured calls win outright: if the provider gave us a
    real tool-call channel there is nothing to scrape, and a model that
    also narrates what it is about to do must not have that narration
    parsed as a second call. Only when the native channel is empty does
    the ``standard`` text parser run, and only if one is declared.

    Set ``native=False`` to force the text path (used by tests that
    exercise a dialect directly).
    """
    if native:
        hits = extract_native_calls(raw)
        if hits:
            return hits
    if not standard:
        return []
    parser = _ALL_PARSERS.get(standard.lower())
    if parser is None:
        return []
    text = _text_of(raw)
    if text is None:
        return []
    return parser(text)


def extract_tool_call(raw: Any, standard: str) -> dict[str, Any] | None:
    """Run the ``standard``'s parser over a Neuron's raw output.

    Accepts the LLM-source shape ``{"response": text}`` or a plain
    string; anything else has no text to parse. Returns the normalised
    call or None. Kept as the single-call entry point;
    :func:`extract_tool_calls` returns all of them.
    """
    return _first(extract_tool_calls(raw, standard))



# ---------------------------------------------------------------------------
# Feeding the observation back
# ---------------------------------------------------------------------------
# A tool result is not prose. Every provider is trained on a specific
# turn pair - the assistant's call, then a result message carrying the
# SAME id - and flattening that into "Tool read returned: ..." throws
# away the correlation the model was trained to rely on. That is the
# usual reason a model re-issues a call it already made.


def _content_str(value: Any) -> str:
    """Result payloads go on the wire as text. Structured results are
    serialised rather than str()'d so the model sees valid JSON it can
    read fields out of."""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return str(value)


def _exchange_ids(exchanges: list[dict[str, Any]]) -> list[str]:
    """One correlation id per exchange, synthesising the missing ones.

    Hermes tags carry no id, and OpenAI rejects a tool message whose
    tool_call_id it has not seen, so a call without one still needs a
    handle. Synthesised ids are allocated against the ids already in
    play: a fixed ``call_<index>`` scheme collides the moment a provider
    hands back a real id of that form, and a collision here silently
    pairs a result with the WRONG call, which is worse than no id at
    all."""
    used = {
        ex.get("call_id") for ex in exchanges
        if isinstance(ex.get("call_id"), str) and ex.get("call_id")
    }
    out: list[str] = []
    for i, ex in enumerate(exchanges):
        cid = ex.get("call_id")
        if isinstance(cid, str) and cid:
            out.append(cid)
            continue
        n = i
        cand = f"call_auto_{n}"
        while cand in used:
            n += 1
            cand = f"call_auto_{n}"
        used.add(cand)
        out.append(cand)
    return out


def tool_result_messages(
    exchanges: list[dict[str, Any]],
    dialect: str,
) -> list[dict[str, Any]]:
    """Turn tool observations into the message pair ``dialect`` expects.

    Each exchange is the Axon's AGENT_OUTPUT observation payload -
    ``{"tool", "args", "call_id", "result" | "error"}`` - so the output
    of a tool step feeds straight back in as the history of the next one.
    Returns the messages to append to the conversation, assistant turn
    first, correlated by id.

    ``openai`` / ``codex`` / ``hermes``  assistant ``tool_calls`` +
                                        ``role: "tool"`` with
                                        ``tool_call_id``
    ``anthropic`` / ``claude``          assistant ``tool_use`` block +
                                        user ``tool_result`` block with
                                        ``tool_use_id``
    ``ollama``                          assistant ``tool_calls`` +
                                        ``role: "tool"`` (no ids)
    ``hermes_text``                     the ChatML prose form, for a
                                        hermes-trained model on a server
                                        with no native tool channel
    """
    d = dialect.lower()
    if not exchanges:
        return []

    ids = _exchange_ids(exchanges)

    if d in ("anthropic", "claude"):
        uses: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []
        for cid, ex in zip(ids, exchanges, strict=True):
            uses.append({
                "type": "tool_use", "id": cid,
                "name": ex.get("tool", ""), "input": ex.get("args") or {},
            })
            block: dict[str, Any] = {
                "type": "tool_result", "tool_use_id": cid,
                "content": _content_str(
                    ex.get("error") or ex.get("result")
                ),
            }
            if ex.get("error"):
                block["is_error"] = True
            results.append(block)
        # Anthropic requires every tool_result for one assistant turn to
        # arrive in a SINGLE user message, in the same order.
        return [
            {"role": "assistant", "content": uses},
            {"role": "user", "content": results},
        ]

    if d == "hermes_text":
        out: list[dict[str, Any]] = []
        for ex in exchanges:
            call = json.dumps({"name": ex.get("tool", ""),
                               "arguments": ex.get("args") or {}})
            body = _content_str(ex.get("error") or ex.get("result"))
            out.append({"role": "assistant",
                        "content": f"<tool_call>\n{call}\n</tool_call>"})
            out.append({"role": "user",
                        "content": f"<tool_response>\n{body}\n</tool_response>"})
        return out

    # OpenAI-compatible (and Ollama, which mirrors it minus the ids).
    ollama = d == "ollama"
    calls: list[dict[str, Any]] = []
    tool_msgs: list[dict[str, Any]] = []
    for cid, ex in zip(ids, exchanges, strict=True):
        fn: dict[str, Any] = {
            "name": ex.get("tool", ""),
            "arguments": (ex.get("args") or {}) if ollama
            else json.dumps(ex.get("args") or {}),
        }
        entry: dict[str, Any] = {"function": fn}
        if not ollama:
            entry["id"] = cid
            entry["type"] = "function"
        calls.append(entry)
        msg: dict[str, Any] = {
            "role": "tool",
            "content": _content_str(ex.get("error") or ex.get("result")),
        }
        if not ollama:
            msg["tool_call_id"] = cid
        else:
            msg["name"] = ex.get("tool", "")
        tool_msgs.append(msg)
    return [
        {"role": "assistant", "content": None, "tool_calls": calls},
        *tool_msgs,
    ]
