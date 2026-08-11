"""
cosmonapse.effector.schema
~~~~~~~~~~~~~~~~~~~~~~~~~~
Tool schemas: the caller-side description of what a bound Effector's
tools accept, and the renderers that turn one description into each
provider's native ``tools=`` payload.

Why caller-side. An Effector is a remote process reachable only through
TOOL_CALL/TOOL_RESULT, so asking it what its tools look like would need
a discovery round trip - a new wire type, and a protocol change. The
schema is instead declared next to the wiring it belongs to::

    EffectorBinding(
        name="files", directed_id="files-effector",
        schemas=[
            ToolSchema("read", "Read a file.",
                       {"path": {"type": "string"}}, required=["path"]),
        ],
    )

The Axon renders those into the dialect its Neuron speaks and injects
them as ``input["tools"]``; the provider then applies constrained
decoding to the tool channel and the reply comes back as a structured
call rather than scraped prose. ``tools=`` on the binding is derived
from the schemas when it is not given, so the routing table and the
advertised surface cannot drift apart.

Nothing here is required. A binding with no schemas behaves exactly as
before: no native payload, text parsing only.
"""

from __future__ import annotations

import inspect
import types
import typing
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ToolSchema",
    "render_tools",
    "tool_schema",
    "validate_args",
]


# ---------------------------------------------------------------------------
# The schema
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSchema:
    """One tool's name, description and JSON Schema parameter object.

    ``properties`` is the JSON Schema ``properties`` map; ``required``
    is the list of parameter names that must be present. The full
    parameter object (``{"type": "object", "properties": ...}``) is
    built by :meth:`parameters` - callers give the interesting half.

    ``strict`` rejects arguments the schema does not declare. Off by
    default: models routinely bolt an extra key onto an otherwise
    perfect call, and failing that call is worse than ignoring the key.
    """

    name: str
    description: str = ""
    properties: dict[str, Any] = field(default_factory=dict)
    required: tuple[str, ...] = ()
    strict: bool = False

    def __post_init__(self) -> None:
        if not self.name or not isinstance(self.name, str):
            raise ValueError("ToolSchema requires a non-empty string name")
        if not isinstance(self.properties, dict):
            raise TypeError(
                f"ToolSchema {self.name!r}: properties must be a dict of "
                f"JSON Schema property definitions"
            )
        missing = [r for r in self.required if r not in self.properties]
        if missing:
            raise ValueError(
                f"ToolSchema {self.name!r}: required names {missing} are "
                f"not in properties {sorted(self.properties)}"
            )
        object.__setattr__(self, "required", tuple(self.required))

    def parameters(self) -> dict[str, Any]:
        """The JSON Schema object describing this tool's arguments."""
        params: dict[str, Any] = {
            "type": "object",
            "properties": dict(self.properties),
        }
        if self.required:
            params["required"] = list(self.required)
        if self.strict:
            params["additionalProperties"] = False
        return params

    # -- provider shapes -------------------------------------------------

    def to_openai(self) -> dict[str, Any]:
        """OpenAI / vLLM / TGI / any OpenAI-compatible ``tools`` entry."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters(),
            },
        }

    def to_anthropic(self) -> dict[str, Any]:
        """Anthropic Messages API ``tools`` entry (``input_schema``)."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters(),
        }

    def to_ollama(self) -> dict[str, Any]:
        """Ollama mirrors the OpenAI function shape."""
        return self.to_openai()


#: dialect -> the ToolSchema method that renders it.
_RENDERERS: dict[str, str] = {
    "openai": "to_openai",
    "codex": "to_openai",
    "hermes": "to_openai",
    "anthropic": "to_anthropic",
    "claude": "to_anthropic",
    "ollama": "to_ollama",
}


def render_tools(
    schemas: list[ToolSchema] | tuple[ToolSchema, ...],
    dialect: str,
) -> list[dict[str, Any]]:
    """Render ``schemas`` into ``dialect``'s native tools payload.

    Accepts either a tool-call standard name (``hermes`` / ``claude`` /
    ``codex``) or a provider name (``openai`` / ``anthropic`` /
    ``ollama``); hermes-trained models are served over OpenAI-compatible
    endpoints, so both spellings resolve to the same shape. An unknown
    dialect renders the OpenAI shape, which is what every
    OpenAI-compatible server expects.
    """
    method = _RENDERERS.get(dialect.lower(), "to_openai")
    return [getattr(s, method)() for s in schemas]


# ---------------------------------------------------------------------------
# Building a schema from a Python function
# ---------------------------------------------------------------------------

_PY_TO_JSON: dict[Any, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def _unwrap_optional(ann: Any) -> tuple[Any, bool]:
    """Strip ``Optional[X]`` / ``X | None``, reporting whether it was one."""
    origin = typing.get_origin(ann)
    if origin is typing.Union or origin is types.UnionType:
        args = [a for a in typing.get_args(ann) if a is not type(None)]
        if len(args) == 1:
            return args[0], True
        return args[0] if args else Any, True
    return ann, False


def _json_type(ann: Any) -> str | None:
    """Best-effort JSON Schema type name for a Python annotation."""
    if ann is inspect.Parameter.empty or ann is Any:
        return None
    ann, _ = _unwrap_optional(ann)
    origin = typing.get_origin(ann)
    if origin is not None:
        ann = origin
    return _PY_TO_JSON.get(ann)


def tool_schema(
    fn: Any,
    *,
    name: str | None = None,
    description: str | None = None,
    params: dict[str, str] | None = None,
    strict: bool = False,
) -> ToolSchema:
    """Build a :class:`ToolSchema` from a Python function.

    The tool name defaults to the function name, the description to the
    first paragraph of its docstring, and each parameter's JSON type to
    its annotation; a parameter with no default is required. ``params``
    supplies per-argument descriptions, which no annotation can carry
    and which materially improve tool selection::

        tool_schema(read_file, params={"path": "path from the workspace root"})

    Untyped parameters are advertised with no ``type`` constraint rather
    than guessed at, so an unannotated helper still produces a usable
    schema. ``self`` and ``cls`` are skipped, as are ``*args`` and
    ``**kwargs``, which have no schema representation.
    """
    sig = inspect.signature(fn)
    hints: dict[str, Any] = {}
    try:
        hints = typing.get_type_hints(fn)
    except Exception:
        hints = {}

    descs = params or {}
    properties: dict[str, Any] = {}
    required: list[str] = []

    for pname, p in sig.parameters.items():
        if pname in ("self", "cls"):
            continue
        if p.kind in (inspect.Parameter.VAR_POSITIONAL,
                      inspect.Parameter.VAR_KEYWORD):
            continue
        prop: dict[str, Any] = {}
        jtype = _json_type(hints.get(pname, p.annotation))
        if jtype is not None:
            prop["type"] = jtype
        if pname in descs:
            prop["description"] = descs[pname]
        properties[pname] = prop
        if p.default is inspect.Parameter.empty:
            required.append(pname)

    if description is None:
        doc = inspect.getdoc(fn) or ""
        description = doc.split("\n\n", 1)[0].strip()

    return ToolSchema(
        name=name or getattr(fn, "__name__", None) or "tool",
        description=description,
        properties=properties,
        required=tuple(required),
        strict=strict,
    )


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------

#: JSON Schema type -> the Python types that satisfy it. bool is excluded
#: from the numeric entries on purpose: in Python ``True`` is an int, and
#: accepting it as one turns a wrong-type call into a silently wrong tool
#: run.
_TYPE_CHECKS: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list, tuple),
    "object": (dict,),
    "null": (type(None),),
}


def _type_ok(value: Any, jtype: str) -> bool:
    allowed = _TYPE_CHECKS.get(jtype)
    if allowed is None:
        return True
    if jtype in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, allowed)


def validate_args(schema: ToolSchema, args: dict[str, Any]) -> str | None:
    """Check ``args`` against ``schema``; return an error string or None.

    Deliberately shallow: required keys, declared scalar types, and
    ``enum`` membership. Nested object validation is not attempted -
    the goal is to catch the calls a model actually gets wrong (a
    forgotten required argument, a number sent as a string) and hand it
    back a sentence it can correct from, not to be a JSON Schema engine.

    The returned string is fed to the model as the tool observation, so
    it names what is wrong and what was expected.
    """
    if not isinstance(args, dict):
        return f"arguments for {schema.name!r} must be an object"

    missing = [r for r in schema.required if r not in args]
    if missing:
        return (
            f"missing required argument(s) {missing} for {schema.name!r}; "
            f"expected {sorted(schema.properties)}"
        )

    problems: list[str] = []
    for key, value in args.items():
        prop = schema.properties.get(key)
        if prop is None:
            if schema.strict:
                problems.append(
                    f"unknown argument {key!r} (expected one of "
                    f"{sorted(schema.properties)})"
                )
            continue
        jtype = prop.get("type")
        if isinstance(jtype, str) and not _type_ok(value, jtype):
            problems.append(
                f"argument {key!r} must be {jtype}, got "
                f"{type(value).__name__}"
            )
        choices = prop.get("enum")
        if isinstance(choices, list) and choices and value not in choices:
            problems.append(
                f"argument {key!r} must be one of {choices}, got {value!r}"
            )

    if problems:
        return f"invalid arguments for {schema.name!r}: " + "; ".join(problems)
    return None
