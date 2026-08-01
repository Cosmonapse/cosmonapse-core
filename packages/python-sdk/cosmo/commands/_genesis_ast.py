"""
cosmo.commands._genesis_ast
~~~~~~~~~~~~~~~~~~~~~~~~~~~
The model behind Genesis's interactive Code tab: read a component module
into a *structured* view (a declaration you can render as a form, and a list
of decorator-registered behaviours you can edit one box at a time), and
write edits back into the file surgically.

Why AST and not a template
--------------------------
Cosmonapse is a decorator/emitter model at the bare bones, which means a
component module has exactly two interesting kinds of top-level statement:

    ENGRAM = Engram.serve(engram_id="notes", ...)   <- the declaration
                                                       (a config form)

    @ENGRAM.on_recall                               <- a behaviour
    async def recall(query):                           (a code box)
        ...

Everything else - imports, module docstring, helper functions someone wrote
by hand - is *theirs*. So this module never regenerates a file. It parses,
locates the exact line span of the one thing being edited, and replaces only
that span; every other byte survives. A file that Genesis can't fully
understand still opens: the parts it recognises become forms and boxes, the
rest is surfaced verbatim and read-only.

Nothing here writes to disk - callers (cosmo/commands/_genesis.py) get new
source text back and decide what to do with it. Every mutating function
re-parses its own output and raises EditError rather than returning source
that doesn't compile.
"""

from __future__ import annotations

import ast
import re
from typing import Any

# The module-level names Genesis treats as "the component this file
# declares". A module is free to build other objects; these are the ones the
# scaffold and the SDK docs use, and the ones brain.py attaches.
TARGET_NAMES = ("AXON", "EFFECTOR", "ENGRAM", "RECEPTOR")

#: Module-level name holding the storage an Engram front delegates to.
BACKEND_NAME = "_backend"

#: What each conventional target name is. A module is free to build a
#: component from its own subclass - the SDK explicitly encourages it for tool
#: families that need their own connect()/close() lifecycle - so the *name*
#: a project assigns to is a far better signal than the constructor it calls.
TARGET_KIND = {
    "AXON": "neuron",
    "EFFECTOR": "effector",
    "ENGRAM": "engram",
    "RECEPTOR": "receptor",
}

#: SDK base classes a project subclasses to build its own component type.
SDK_BASES = {
    "Axon": "neuron",
    "Effector": "effector",
    "Engram": "engram",
    "InMemoryEngram": "engram",
    "SqliteEngram": "engram",
    "PostgresEngram": "engram",
    "Receptor": "receptor",
    "CliReceptor": "receptor",
    "ApiReceptor": "receptor",
    "ChatReceptor": "receptor",
}

# Callee dotted-name -> (kind, shape). Shape drives which protocols the
# component can host, which is why an Engram's shape is a first-class thing
# the UI can switch: InMemoryEngram is a finished backend with no hooks,
# Engram.serve() is a hook surface with no storage.
_CALLEES: dict[str, tuple[str, str]] = {
    "Axon": ("neuron", "axon"),
    "Axon.from_source": ("neuron", "axon"),
    "Axon.ollama": ("neuron", "axon"),
    "Axon.huggingface": ("neuron", "axon"),
    "Axon.hf": ("neuron", "axon"),
    "Axon.openai": ("neuron", "axon"),
    "Axon.anthropic": ("neuron", "axon"),
    "Axon.mcp": ("neuron", "axon"),
    "Effector.serve": ("effector", "served"),
    "Engram.serve": ("engram", "served"),
    "InMemoryEngram": ("engram", "prebuilt"),
    "SqliteEngram": ("engram", "prebuilt"),
    "PostgresEngram": ("engram", "prebuilt"),
    # A Receptor's shape is simply which of the three it is. Unlike an
    # Engram's shape it is not switchable in place: the three take different
    # constructor keywords and expose different decorators, so changing one
    # into another is a rewrite, not a toggle.
    "CliReceptor": ("receptor", "cli"),
    "ApiReceptor": ("receptor", "api"),
    "ChatReceptor": ("receptor", "chat"),
}

#: Decorators whose first argument is conventionally written positionally,
#: mapped to the keyword it corresponds to.
#:
#: Everything else Genesis models is keyword-only, so ``if dec.args: return
#: None`` was a safe blanket rule until Receptors arrived. Both of the
#: decorators below read wrong in keyword form - ``@RECEPTOR.command("ping")``
#: and ``@RECEPTOR.route("/memory")`` are how the SDK docstrings, the examples
#: and the generated scaffold all write them - and refusing them would leave
#: the default project's own ``ping`` command uneditable in the Code tab.
#:
#: Only these two names are affected; no other primitive has a decorator that
#: takes a positional.
POSITIONAL_DECORATOR_ARG = {"command": "name", "route": "path"}

#: The three Receptor flavours, in the order the UI offers them. CLI leads
#: because it is the one with no optional dependency.
RECEPTOR_SHAPES = ("cli", "api", "chat")

#: Which Receptor class each shape is written with.
RECEPTOR_CLASSES = {"cli": "CliReceptor", "api": "ApiReceptor", "chat": "ChatReceptor"}

#: Source-paired Axon classmethod -> the Neuron provider it builds.
#: ``Axon.from_source`` is deliberately absent: its provider is an argument,
#: not part of the method name, so it's read off the call instead.
AXON_SOURCE_CALLEES: dict[str, str] = {
    "Axon.ollama": "ollama",
    "Axon.huggingface": "huggingface",
    "Axon.hf": "huggingface",
    "Axon.openai": "openai",
    "Axon.anthropic": "anthropic",
    "Axon.mcp": "mcp",
}

#: Every provider Neuron(source=...) registers, plus "custom" for an Axon
#: wrapping a function this project wrote. The order is the order the UI
#: offers them in.
AXON_SOURCES = (
    "custom", "ollama", "huggingface", "openai", "anthropic",
    "groq", "openrouter", "together", "mistral", "mcp",
)

#: How the pairing is written. ``explicit`` supplies its own ``neuron_fn``;
#: the other two let ``from_source`` build the Neuron, which is also what
#: attaches the recogniser and teaches the cosmo intent convention.
AXON_FORMS = ("explicit", "paired", "from_source")

#: Providers with no sugar classmethod - they can only be written through
#: ``Axon.from_source``.
_NO_ALIAS = frozenset({"groq", "openrouter", "together", "mistral"})

#: Keywords only the explicit form accepts. ``from_source`` builds the
#: Neuron and computes the parser itself, so passing either is a TypeError.
_EXPLICIT_ONLY = ("neuron_fn", "output_parser")

#: Prebuilt Engram class -> the UI's backend id, and back.
ENGRAM_BACKENDS = {
    "InMemoryEngram": "in-memory",
    "SqliteEngram": "sqlite",
    "PostgresEngram": "postgres",
}
ENGRAM_BACKEND_CLASSES = {v: k for k, v in ENGRAM_BACKENDS.items()}


class EditError(Exception):
    """An edit was rejected - bad syntax, unknown target, unsafe rewrite."""


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def _dotted(node: ast.AST) -> str | None:
    """Render a Name/Attribute chain as 'Engram.serve', or None."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _assigned(node: ast.AST) -> tuple[list[str], ast.Call | None]:
    """Names bound by an assignment, and the call on its right-hand side.

    Covers both ``AXON = Axon(...)`` and the annotated ``AXON: Axon =
    Axon(...)`` a typed codebase writes - the second is an ``AnnAssign``, a
    different node entirely, and treating only ``Assign`` as an assignment is
    how a perfectly ordinary declaration comes to be invisible.
    """
    if isinstance(node, ast.Assign):
        targets = node.targets
        value = node.value
    elif isinstance(node, ast.AnnAssign):
        targets = [node.target]
        value = node.value
    else:
        return [], None
    if not isinstance(value, ast.Call):
        return [], None
    return [t.id for t in targets if isinstance(t, ast.Name)], value


def _lines(text: str) -> list[str]:
    return text.splitlines(keepends=True)


def _span(text: str, node: ast.AST) -> tuple[int, int]:
    """Character offsets [start, end) of a node's full lines."""
    lines = _lines(text)
    start = sum(len(l) for l in lines[: node.lineno - 1])
    end = sum(len(l) for l in lines[: node.end_lineno])
    return start, end


def _value_of(node: ast.AST, source: str) -> dict[str, Any]:
    """Classify a keyword's value so the UI knows what widget to draw.

    Anything that isn't a plain literal comes back as ``expr`` carrying its
    exact source text: the form shows it read-only rather than pretending a
    text input could round-trip ``[EngramBinding(name="notes", ...)]``.
    """
    if isinstance(node, ast.Constant):
        v = node.value
        if isinstance(v, str):
            return {"type": "string", "value": v}
        if isinstance(v, bool):
            return {"type": "bool", "value": v}
        if isinstance(v, (int, float)):
            return {"type": "number", "value": v}
        if v is None:
            return {"type": "none", "value": None}
    if isinstance(node, (ast.List, ast.Tuple)):
        items = node.elts
        if all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in items):
            return {
                "type": "string_list",
                "value": [e.value for e in items],  # type: ignore[attr-defined]
            }
    if isinstance(node, ast.Name):
        return {"type": "name", "value": node.id}
    return {"type": "expr", "value": ast.get_source_segment(source, node) or ""}


def _render_value(field: dict[str, Any]) -> str:
    """Inverse of _value_of - back to Python source."""
    t = field.get("type")
    v = field.get("value")
    if t == "string":
        return _py_str(str(v))
    if t == "bool":
        return "True" if v else "False"
    if t == "number":
        return repr(v)
    if t == "none":
        return "None"
    if t == "string_list":
        items = [str(x) for x in (v or [])]
        if not items:
            return "[]"
        return "[" + ", ".join(_py_str(i) for i in items) + "]"
    if t in ("name", "expr"):
        return str(v)
    raise EditError(f"can't render a {t!r} value")


def _py_str(s: str) -> str:
    """A double-quoted Python string literal (the house style)."""
    body = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{body}"'


def _decorator_info(dec: ast.AST, source: str) -> dict[str, Any] | None:
    """Recognise ``@TARGET.proto`` / ``@TARGET.proto(...)`` / ``@TARGET.host.proto(...)``.

    Returns the scope ("own" or "host"), the protocol name, and any decorator
    keyword arguments (``neuron=``, ``capability=``, ``trace_id=``,
    ``every_s=``) - which the UI renders as the behaviour's filter fields.
    """
    call_kwargs: dict[str, Any] = {}
    positional: list[ast.expr] = []
    if isinstance(dec, ast.Call):
        target = dec.func
        for kw in dec.keywords:
            if kw.arg is None:
                return None  # @deco(**something) - not a shape we model
            call_kwargs[kw.arg] = _value_of(kw.value, source)
        positional = list(dec.args)
    else:
        target = dec

    dotted = _dotted(target)
    if not dotted:
        return None
    parts = dotted.split(".")
    if positional:
        # One positional is allowed, and only for the decorators that are
        # conventionally written that way - it is folded into the keyword it
        # stands for so the form has a single representation to edit.
        pos_key = POSITIONAL_DECORATOR_ARG.get(parts[-1])
        if pos_key is None or len(positional) > 1 or pos_key in call_kwargs:
            return None
        call_kwargs[pos_key] = _value_of(positional[0], source)
    if parts[0] not in TARGET_NAMES:
        return None
    if len(parts) == 2:
        return {"scope": "own", "protocol": parts[1], "args": call_kwargs,
                "target": parts[0]}
    if len(parts) == 3 and parts[1] == "host":
        return {"scope": "host", "protocol": parts[2], "args": call_kwargs,
                "target": parts[0]}
    return None


def _signature(fn: ast.FunctionDef | ast.AsyncFunctionDef, source: str) -> str:
    seg = ast.get_source_segment(source, fn) or ""
    m = re.search(r"\(", seg)
    if not m:
        return ""
    # Walk to the matching close paren so nested parens/defaults survive.
    depth = 0
    for i in range(m.start(), len(seg)):
        if seg[i] == "(":
            depth += 1
        elif seg[i] == ")":
            depth -= 1
            if depth == 0:
                return seg[m.start() + 1 : i].strip()
    return ""


def _body_text(text: str, fn: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[str, bool]:
    """The function body's source, dedented when that is provably safe.

    Returns ``(body, dedented)``. A small code box reads much better without
    a permanent 4-space gutter, but dedenting by guessing a common prefix
    would corrupt a triple-quoted string that has a line at column 0. So the
    rule is deliberately blunt: dedent only when *every* non-blank line
    starts with four spaces. Otherwise hand back the raw slice and let the
    client round-trip it verbatim.
    """
    lines = _lines(text)
    first = fn.body[0].lineno - 1
    # A docstring-only first statement still starts the body; use the line
    # after the signature's close paren, which is body[0].lineno.
    raw = "".join(lines[first : fn.end_lineno])
    stripped = raw.rstrip("\n")
    if not stripped:
        return "", False
    body_lines = stripped.split("\n")
    if all(not l.strip() or l.startswith("    ") for l in body_lines):
        return "\n".join(l[4:] if l.strip() else "" for l in body_lines), True
    return stripped, False


def _factory_declaration(
    node: ast.FunctionDef | ast.AsyncFunctionDef, text: str,
) -> dict[str, Any] | None:
    """A component built and returned by a factory, rather than assigned.

    Several real projects parameterise a component instead of declaring one -
    ``neurons/pool.py`` in the round-robin example is ``def make_axon(neuron_id)
    -> Axon: return Axon(neuron_id=neuron_id, ...)``, so N identical Neurons can
    be spun up from one module. There is no module-level object, which means
    no decorators can attach, but the constructor is still a config form worth
    having. Only a ``return <Callee>(...)`` at the function's top level counts;
    anything conditional is left alone.
    """
    for stmt in node.body:
        if not isinstance(stmt, ast.Return):
            continue
        call = stmt.value if isinstance(stmt.value, ast.Call) else None
        anchor: ast.AST = stmt
        if call is None and isinstance(stmt.value, ast.Name):
            # `axon = Axon(...)` then `return axon` - just as common as
            # returning the constructor directly, and the same declaration.
            wanted = stmt.value.id
            for earlier in node.body:
                names, value = _assigned(earlier)
                if value is not None and wanted in names:
                    call, anchor = value, earlier
        if call is None:
            continue
        callee = _dotted(call.func) or ""
        info = _CALLEES.get(callee)
        if not info:
            continue
        kind, shape = info
        return {
            "target": node.name,
            "callee": callee,
            "kind": kind,
            "shape": shape,
            "scope": "factory",
            "factory": node.name,
            "fields": _fields_of(call, text),
                    "verbatim": _verbatim_args(call, text),
            "prefix": "return " if anchor is stmt else f"{_assigned(anchor)[0][0]} = ",
            "lineno": anchor.lineno,
            "end_lineno": anchor.end_lineno,
            "indent": " " * anchor.col_offset,
        }
    return None


def _defined_bases(tree: ast.Module) -> list[dict[str, Any]]:
    """Component classes this module *defines* (as opposed to instantiates).

    ``engram/keyword_engram.py`` is ``class KeywordEngram(Engram)`` with no
    module-level instance - it's a backend other modules construct. There is
    no declaration to put in a form, and saying so beats reporting the file as
    unrecognised.
    """
    out = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        for base in node.bases:
            dotted = _dotted(base) or ""
            leaf = dotted.rsplit(".", 1)[-1]
            if leaf in SDK_BASES:
                out.append({"name": node.name, "base": leaf, "kind": SDK_BASES[leaf]})
                break
    return out


def parse_component(text: str) -> dict[str, Any]:
    """Read a component module into the Code tab's structured view."""
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        raise EditError(f"{e.msg} (line {e.lineno})") from e

    declaration: dict[str, Any] | None = None
    backend: dict[str, Any] | None = None
    behaviors: list[dict[str, Any]] = []
    claimed: set[int] = set()
    async_fns: list[str] = []
    factories: list[tuple[dict[str, Any], Any]] = []
    loose: list[tuple[str, str, tuple[str, str], Any, ast.Call]] = []

    for node in tree.body:
        names, call = _assigned(node)
        if call is not None:
            callee = _dotted(call.func) or ""
            info = _CALLEES.get(callee)
            target_name = next((n for n in names if n in TARGET_NAMES), None)
            if info is None and target_name is not None:
                # A project's own Effector/Engram/Axon subclass. Genesis can't
                # know what hooks it carries, but it is unambiguously the
                # component this module declares, and its constructor keywords
                # are still a config form.
                info = (TARGET_KIND[target_name], "custom")
            if info and target_name is None and names:
                loose.append((names[0], callee, info, node, call))
            if info and target_name is not None:
                kind, shape = info
                declaration = {
                    "target": target_name,
                    "callee": callee,
                    "kind": kind,
                    "shape": shape,
                    "scope": "module",
                    "factory": None,
                    "fields": _fields_of(call, text),
                    "verbatim": _verbatim_args(call, text),
                    "lineno": node.lineno,
                    "end_lineno": node.end_lineno,
                }
                claimed.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
                continue
            if info and BACKEND_NAME in names:
                backend = {
                    "name": BACKEND_NAME,
                    "callee": callee,
                    "backend": ENGRAM_BACKENDS.get(callee, callee),
                    "fields": _fields_of(call, text),
                    "verbatim": _verbatim_args(call, text),
                    "lineno": node.lineno,
                    "end_lineno": node.end_lineno,
                }
                claimed.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
                continue

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if isinstance(node, ast.AsyncFunctionDef):
                async_fns.append(node.name)
            if not node.decorator_list:
                found = _factory_declaration(node, text)
                if found:
                    factories.append((found, node))
            matched = [_decorator_info(d, text) for d in node.decorator_list]
            hits = [m for m in matched if m]
            if hits and len(hits) == len(node.decorator_list):
                info = hits[0]
                body, dedented = _body_text(text, node)
                behaviors.append({
                    "id": f"{info['scope']}:{info['protocol']}:{node.name}",
                    "scope": info["scope"],
                    "protocol": info["protocol"],
                    "target": info["target"],
                    "args": info["args"],
                    "fn_name": node.name,
                    "is_async": isinstance(node, ast.AsyncFunctionDef),
                    "signature": _signature(node, text),
                    "body": body,
                    "dedented": dedented,
                    "lineno": node.decorator_list[0].lineno,
                    "end_lineno": node.end_lineno,
                })
                claimed.update(
                    range(node.decorator_list[0].lineno, (node.end_lineno or node.lineno) + 1),
                )

    # A module-level declaration always wins; a factory is the fallback, and
    # it keeps its enclosing def visible in "other" so the reader sees the
    # whole function, not just the constructor call inside it.
    if declaration is None and factories:
        declaration = factories[0][0]
    if declaration is None and loose:
        # Nothing named AXON/EFFECTOR/ENGRAM, but the module clearly builds a
        # component. Better to configure the one it built than to show nothing.
        name, callee, (kind, shape), node, call = loose[0]
        declaration = {
            "target": name, "callee": callee, "kind": kind, "shape": shape,
            "scope": "module", "factory": None,
            "fields": _fields_of(call, text),
                    "verbatim": _verbatim_args(call, text),
            "lineno": node.lineno, "end_lineno": node.end_lineno,
        }

    defines = _defined_bases(tree)
    _annotate_axon(declaration)

    return {
        "kind": declaration["kind"] if declaration else (defines[0]["kind"] if defines else None),
        "shape": _effective_shape(declaration, backend),
        "defines": defines,
        "declaration": declaration,
        "backend": backend,
        "behaviors": behaviors,
        "async_fns": async_fns,
        "other": _unclaimed(text, tree, claimed),
    }


def _annotate_axon(declaration: dict[str, Any] | None) -> None:
    """Record which Neuron provider backs an Axon, and how it was written.

    Two axes, because they answer different questions and the config form
    needs both. ``source`` is the provider - it decides which kwargs the
    call accepts. ``form`` is the syntax - it decides whether the Axon also
    gets a recogniser and the intent system prompt, which only the
    ``from_source`` path attaches. A hand-written Neuron is ``source
    "custom"`` and always ``form "explicit"``.

    Mutates in place, and only for Axons: an Engram's equivalent axes are
    already ``shape`` and ``backend``.
    """
    if declaration is None or declaration.get("kind") != "neuron":
        return
    callee = declaration["callee"]
    if callee in AXON_SOURCE_CALLEES:
        declaration["source"] = AXON_SOURCE_CALLEES[callee]
        declaration["form"] = "paired"
        return
    if callee == "Axon.from_source":
        declaration["source"] = _source_argument(declaration)
        declaration["form"] = "from_source"
        return
    declaration["source"] = "custom"
    declaration["form"] = "explicit"


def _source_argument(declaration: dict[str, Any]) -> str:
    """``Axon.from_source``'s provider, written either way round.

    It is positional in every example and in the SDK docstrings, but the
    parameter is positional-*or*-keyword, so ``source="ollama"`` is equally
    valid and a form that only looked at ``args`` would report no provider
    at all for it.
    """
    for f in declaration.get("fields", []):
        if f["name"] == "source" and f["type"] == "string":
            return str(f["value"])
    positional = (declaration.get("verbatim") or ([], []))[0]
    if positional:
        try:
            value = ast.literal_eval(positional[0])
        except (ValueError, SyntaxError):
            return ""
        if isinstance(value, str):
            return value
    return ""


def _effective_shape(declaration: dict | None, backend: dict | None) -> str | None:
    if declaration is None:
        return None
    shape = declaration["shape"]
    if declaration["kind"] == "engram" and shape == "served" and backend:
        return "served-over-backend"
    return shape


def _fields_of(call: ast.Call, source: str) -> list[dict[str, Any]]:
    """The keyword arguments a config form can edit."""
    fields = []
    for kw in call.keywords:
        if kw.arg is None:
            continue
        f = _value_of(kw.value, source)
        f["name"] = kw.arg
        fields.append(f)
    return fields


def _verbatim_args(call: ast.Call, source: str) -> tuple[list[str], list[str]]:
    """Arguments the form doesn't model, kept as source so they survive a save.

    ``Axon.ollama("my-neuron", model="llama3")`` passes the neuron_id
    positionally, and ``**overrides`` is a legal thing to write. Neither is a
    form field, but rendering the call from the keywords alone would delete
    them - a save that silently breaks the module is far worse than a field
    the form can't edit. Returns (positional, double-starred) as source text,
    in their original order.
    """
    positional = [ast.get_source_segment(source, a) or "" for a in call.args]
    starred = [
        f"**{ast.get_source_segment(source, kw.value) or ''}"
        for kw in call.keywords if kw.arg is None
    ]
    return positional, starred


def _unclaimed(text: str, tree: ast.Module, claimed: set[int]) -> list[dict[str, Any]]:
    """Top-level code Genesis doesn't model, as contiguous read-only chunks."""
    lines = _lines(text)
    out: list[dict[str, Any]] = []
    for node in tree.body:
        start, end = node.lineno, node.end_lineno or node.lineno
        if any(n in claimed for n in range(start, end + 1)):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str) and node is tree.body[0]:
            label = "Module docstring"
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            label = "Imports"
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            label = f"def {node.name}"
        elif isinstance(node, ast.ClassDef):
            label = f"class {node.name}"
        else:
            label = "Module code"
        chunk = "".join(lines[start - 1 : end]).rstrip("\n")
        if out and out[-1]["label"] == label == "Imports":
            out[-1]["text"] += "\n" + chunk
            continue
        out.append({"label": label, "text": chunk, "lineno": start})
    return out


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def _validate(text: str) -> str:
    try:
        ast.parse(text)
    except SyntaxError as e:
        raise EditError(f"the edit would break the file: {e.msg} (line {e.lineno})") from e
    return text


def _replace_span(text: str, lineno: int, end_lineno: int, replacement: str) -> str:
    lines = _lines(text)
    start = sum(len(l) for l in lines[: lineno - 1])
    end = sum(len(l) for l in lines[:end_lineno])
    if not replacement.endswith("\n"):
        replacement += "\n"
    return text[:start] + replacement + text[end:]


def _render_call(
    target: str,
    callee: str,
    fields: list[dict[str, Any]],
    positional: list[str] | None = None,
    starred: list[str] | None = None,
) -> str:
    """``TARGET = Callee(...)`` over several lines - the scaffold's house style."""
    return _render_stmt(f"{target} = ", callee, fields, "", positional, starred)


def _render_stmt(
    prefix: str,
    callee: str,
    fields: list[dict[str, Any]],
    indent: str = "",
    positional: list[str] | None = None,
    starred: list[str] | None = None,
) -> str:
    """One statement whose value is a constructor call, in house style.

    ``prefix`` is whatever introduces the call - ``"AXON = "`` at module level,
    ``"return "`` inside a factory - and ``indent`` is the column the statement
    sits at, so rewriting a factory's constructor keeps it inside its def.
    ``positional`` and ``starred`` are passed straight through in the only
    order Python accepts, so nothing the form can't edit is lost.
    """
    parts = list(positional or [])
    parts += [f"{f['name']}={_render_value(f)}" for f in fields]
    parts += list(starred or [])
    if not parts:
        return f"{indent}{prefix}{callee}()"
    body = "".join(f"{indent}    {part}," + "\n" for part in parts)
    return f"{indent}{prefix}{callee}(" + "\n" + body + f"{indent})"


def edit_declaration(text: str, fields: list[dict[str, Any]]) -> str:
    """Rewrite the component's constructor keywords from the config form.

    Only the keyword list changes: the assignment target and the callee stay
    exactly as they were, so switching an Engram's backend is a separate,
    explicit operation (see :func:`set_engram_shape`) rather than something
    a form field can do by accident.
    """
    model = parse_component(text)
    decl = model["declaration"]
    if not decl:
        raise EditError("this file has no component declaration to edit")
    pos, starred = decl.get("verbatim") or ([], [])
    if decl.get("scope") == "factory":
        rendered = _render_stmt(
            decl.get("prefix", "return "), decl["callee"], fields,
            decl["indent"], pos, starred,
        )
    else:
        rendered = _render_call(decl["target"], decl["callee"], fields, pos, starred)
    return _validate(_replace_span(text, decl["lineno"], decl["end_lineno"], rendered))


def edit_backend(text: str, fields: list[dict[str, Any]]) -> str:
    """Rewrite the delegated storage's constructor keywords."""
    model = parse_component(text)
    backend = model["backend"]
    if not backend:
        raise EditError("this file has no backend to edit")
    b_pos, b_starred = backend.get("verbatim") or ([], [])
    rendered = _render_call(BACKEND_NAME, backend["callee"], fields, b_pos, b_starred)
    return _validate(
        _replace_span(text, backend["lineno"], backend["end_lineno"], rendered),
    )


def _render_behavior(
    *,
    target: str,
    scope: str,
    protocol: str,
    args: list[dict[str, Any]],
    fn_name: str,
    signature: str,
    body: str,
    is_async: bool = True,
    indent: bool = True,
) -> str:
    path = f"{target}.host.{protocol}" if scope == "host" else f"{target}.{protocol}"
    # Write back the one conventionally-positional argument positionally, so a
    # round-trip through Genesis leaves @RECEPTOR.command("ping") looking the
    # way the SDK docs and the scaffold write it.
    pos_key = POSITIONAL_DECORATOR_ARG.get(protocol)
    lead = next((a for a in args if a["name"] == pos_key), None) if pos_key else None
    rest = [a for a in args if a is not lead]
    parts = ([_render_value(lead)] if lead is not None else [])
    parts += [f"{a['name']}={_render_value(a)}" for a in rest]
    if parts:
        deco = f"@{path}({', '.join(parts)})"
    else:
        deco = f"@{path}"

    lines = (body or "").rstrip("\n").split("\n")
    if indent:
        lines = ["    " + l if l.strip() else "" for l in lines]
    if not any(l.strip() for l in lines):
        lines = ["    ..."]
    kw = "async def" if is_async else "def"
    return f"{deco}\n{kw} {fn_name}({signature}):\n" + "\n".join(lines) + "\n"


def upsert_behavior(
    text: str,
    *,
    behavior_id: str | None,
    scope: str,
    protocol: str,
    fn_name: str,
    signature: str,
    body: str,
    args: list[dict[str, Any]] | None = None,
    is_async: bool = True,
    indent: bool = True,
) -> str:
    """Add a behaviour, or replace an existing one in place.

    ``behavior_id`` identifies the block being replaced; pass None to append
    a new one at the end of the module (predictable, and it keeps the
    declaration and imports where the reader expects them).
    """
    model = parse_component(text)
    decl = model["declaration"]
    if not decl:
        raise EditError("this file has no component to attach behaviour to")
    if decl.get("scope") == "factory":
        raise EditError(
            f"{decl['factory']}() builds its component per call, so there is no "
            "module-level object to decorate - add the hook inside the factory, "
            "or assign the component at module level first",
        )
    target = decl["target"]

    if not fn_name.isidentifier():
        raise EditError(f"{fn_name!r} is not a valid Python function name")

    rendered = _render_behavior(
        target=target, scope=scope, protocol=protocol, args=args or [],
        fn_name=fn_name, signature=signature, body=body,
        is_async=is_async, indent=indent,
    )

    existing = next((b for b in model["behaviors"] if b["id"] == behavior_id), None)
    if existing:
        out = _replace_span(text, existing["lineno"], existing["end_lineno"], rendered)
    else:
        clash = next((b for b in model["behaviors"] if b["fn_name"] == fn_name), None)
        if clash:
            raise EditError(
                f"this module already has a handler called {fn_name!r} "
                f"(@{target}.{clash['protocol']}) - give this one another name",
            )
        out = text.rstrip("\n") + "\n\n\n" + rendered
    return _validate(out)


def delete_behavior(text: str, behavior_id: str) -> str:
    """Remove a behaviour block, decorators and all."""
    model = parse_component(text)
    target = next((b for b in model["behaviors"] if b["id"] == behavior_id), None)
    if not target:
        raise EditError(f"no behaviour {behavior_id!r} in this file")
    out = _replace_span(text, target["lineno"], target["end_lineno"], "")
    # Collapse the blank-line run the removal left behind.
    out = re.sub(r"\n{4,}", "\n\n\n", out)
    return _validate(out.rstrip("\n") + "\n")


# ---------------------------------------------------------------------------
# Engram shape - the one structural rewrite Genesis performs
# ---------------------------------------------------------------------------
#
# An Engram is the one primitive with a real choice of shape, because the SDK
# splits storage from hooks:
#
#   prebuilt              InMemoryEngram(...)          working storage, and
#                                                      no decorators at all
#                                                      beyond @ENGRAM.host.*
#   served                Engram.serve(...)            the full hook surface,
#                                                      and no storage
#   served-over-backend   Engram.serve(...) in front    both - the layering
#                         of a module-level _backend    Engram.serve's own
#                                                      docstring describes
#
# The Code tab exposes that as two form fields, and this is what turns the
# choice into code.

ENGRAM_SHAPES = ("prebuilt", "served", "served-over-backend")

_DELEGATE_RECALL = (
    "recall",
    "query, **kw",
    "return await _backend.recall(query, **kw)",
)
_DELEGATE_IMPRINT = (
    "imprint",
    "op, entry, **kw",
    "return await _backend.imprint(op, entry, **kw)",
)

_COSMONAPSE_ENGRAM_NAMES = {"Engram"} | set(ENGRAM_BACKEND_CLASSES.values())


def _import_names_for(shape: str, backend: str) -> list[str]:
    cls = ENGRAM_BACKEND_CLASSES.get(backend, "InMemoryEngram")
    if shape == "prebuilt":
        return [cls]
    if shape == "served":
        return ["Engram"]
    return sorted({"Engram", cls})


def _rewrite_cosmonapse_import(text: str, needed: list[str]) -> str:
    """Keep ``from cosmonapse import ...`` in step with the chosen shape.

    Only the Engram family is touched - anything else the module imports
    from cosmonapse (Axon, Effector, SignalType, ...) is preserved, because
    it isn't ours to reason about.
    """
    tree = ast.parse(text)
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom) or node.module != "cosmonapse":
            continue
        kept = [a.name for a in node.names if a.name not in _COSMONAPSE_ENGRAM_NAMES]
        names = sorted(set(kept) | set(needed))
        line = "from cosmonapse import " + ", ".join(names)
        return _replace_span(text, node.lineno, node.end_lineno or node.lineno, line)
    # No cosmonapse import yet: put one after the last import, or after the
    # module docstring.
    line = "from cosmonapse import " + ", ".join(sorted(needed))
    anchor = None
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            anchor = node
    if anchor is not None:
        return _replace_span(
            text, anchor.lineno, anchor.end_lineno or anchor.lineno,
            "".join(_lines(text)[anchor.lineno - 1 : anchor.end_lineno]).rstrip("\n")
            + "\n" + line,
        )
    lines = _lines(text)
    first = tree.body[0].end_lineno if tree.body else 0
    return "".join(lines[:first]) + line + "\n" + "".join(lines[first:])


def set_engram_shape(text: str, *, shape: str, backend: str = "in-memory") -> str:
    """Convert an Engram module between the three shapes above.

    Refuses rather than silently breaking things: dropping to ``prebuilt``
    with hook-based behaviours present would orphan them (a prebuilt backend
    has nowhere to register them), and dropping the backend while a handler
    still references ``_backend`` would leave a NameError at import time.
    """
    if shape not in ENGRAM_SHAPES:
        raise EditError(f"unknown engram shape {shape!r}")
    if shape != "served" and backend not in ENGRAM_BACKEND_CLASSES:
        raise EditError(f"unknown engram backend {backend!r}")

    model = parse_component(text)
    decl = model["declaration"]
    if not decl or decl["kind"] != "engram":
        raise EditError("this file doesn't declare an Engram")
    if decl["shape"] == "custom":
        raise EditError(
            f"{decl['callee']} is this project's own Engram class - changing "
            "the shape would replace it with an SDK backend and lose whatever "
            "it does. Edit the module directly if that's really the intent.",
        )
    if model["shape"] == shape and (
        shape == "served" or (model["backend"] or {}).get("backend") == backend
    ):
        return text

    own = [b for b in model["behaviors"] if b["scope"] == "own"]
    if shape == "prebuilt" and own:
        names = ", ".join(f"@ENGRAM.{b['protocol']}" for b in own)
        raise EditError(
            f"a prebuilt backend has no hooks to register {names} on - "
            "delete those behaviours first, or keep a served shape",
        )
    if shape == "served":
        users = [b["fn_name"] for b in model["behaviors"] if BACKEND_NAME in b["body"]]
        if users:
            raise EditError(
                f"{', '.join(users)} still call {BACKEND_NAME} - rewrite them "
                "before dropping the backend",
            )

    cls = ENGRAM_BACKEND_CLASSES.get(backend, "InMemoryEngram")
    id_fields = [f for f in decl["fields"] if f["name"] in ("engram_id", "engram_kind")]
    callee = cls if shape == "prebuilt" else "Engram.serve"

    # 1. Drop any existing backend assignment (re-added below if still wanted).
    out = text
    if model["backend"]:
        b = model["backend"]
        out = _replace_span(out, b["lineno"], b["end_lineno"], "")
        out = re.sub(r"\n{4,}", "\n\n\n", out)

    # 2. Rewrite the declaration - re-parsed because step 1 moved the lines.
    model = parse_component(out)
    decl = model["declaration"]
    block = _render_call(decl["target"], callee, decl["fields"] if callee == decl["callee"] else id_fields)
    if shape == "served-over-backend":
        backend_block = _render_call(BACKEND_NAME, cls, id_fields)
        block = backend_block + "\n\n" + block
    out = _replace_span(out, decl["lineno"], decl["end_lineno"], block)

    # 3. Imports follow the shape.
    out = _rewrite_cosmonapse_import(out, _import_names_for(shape, backend))

    # 4. A served-over-backend Engram with no read/write handlers can't
    #    answer anything, so seed the two that just forward to the storage.
    if shape == "served-over-backend":
        have = {b["protocol"] for b in parse_component(out)["behaviors"]}
        for proto, (fn_name, sig, body) in (
            ("on_recall", _DELEGATE_RECALL),
            ("on_imprint", _DELEGATE_IMPRINT),
        ):
            if proto not in have:
                out = upsert_behavior(
                    out, behavior_id=None, scope="own", protocol=proto,
                    fn_name=fn_name, signature=sig, body=body,
                )

    return _validate(out)


# ---------------------------------------------------------------------------
# Axon source
# ---------------------------------------------------------------------------
# The Neuron analogue of set_engram_shape. An Engram switches between storage
# and hooks; an Axon switches between wrapping a function this project wrote
# and wrapping a provider the SDK knows how to build - and, for a provider,
# between the sugar classmethod and the from_source call that reaches the
# four providers with no classmethod of their own.

def default_axon_form(source: str) -> str:
    """The form a source is written in unless the caller says otherwise."""
    if source == "custom":
        return "explicit"
    return "from_source" if source in _NO_ALIAS else "paired"


def axon_callee(source: str, form: str) -> str:
    """The constructor a (source, form) pair is written with."""
    if form == "explicit":
        return "Axon"
    if form == "from_source":
        return "Axon.from_source"
    return next(c for c, s in AXON_SOURCE_CALLEES.items()
                if s == source and c != "Axon.hf")


def set_axon_source(text: str, *, source: str, form: str = "") -> str:
    """Convert an Axon between providers and between build forms.

    Refuses rather than guessing. The dangerous case is argument loss: these
    three constructors do not take the same keywords, so a conversion that
    kept everything would emit a call that raises TypeError on import, and
    one that kept nothing would quietly discard the model the user chose.
    The rule is to carry over every keyword the *destination* accepts, drop
    the ones it provably doesn't (``neuron_fn`` and ``output_parser`` are
    not from_source keywords), and refuse outright when the call carries
    ``**kwargs`` whose contents can't be attributed either way.
    """
    from cosmo.commands import _genesis_protocols as _gp  # lazy: one-way

    if source not in AXON_SOURCES:
        raise EditError(f"unknown neuron source {source!r}")
    form = form or default_axon_form(source)
    if form not in AXON_FORMS:
        raise EditError(f"unknown axon form {form!r}")
    if (form == "explicit") != (source == "custom"):
        raise EditError(
            "a hand-written Neuron is the explicit form and a provider-backed "
            "one isn't - pick source 'custom' with form 'explicit', or a "
            "provider with 'paired' / 'from_source'",
        )
    if form == "paired" and source in _NO_ALIAS:
        raise EditError(
            f"{source} has no Axon.{source}() classmethod - it's an "
            f"OpenAI-compatible endpoint reached through "
            f"Axon.from_source({source!r}, ...)",
        )

    model = parse_component(text)
    decl = model["declaration"]
    if not decl or decl["kind"] != "neuron":
        raise EditError("this file doesn't declare an Axon")
    if decl["shape"] == "custom":
        raise EditError(
            f"{decl['callee']} is this project's own Axon subclass - swapping "
            "it for an SDK constructor would lose whatever it does. Edit the "
            "module directly if that's really the intent.",
        )
    positional, starred = decl.get("verbatim") or ([], [])
    if starred:
        raise EditError(
            "this call passes **kwargs, so Genesis can't tell which arguments "
            "belong to the new source - inline them first",
        )
    if len(positional) > 1:
        raise EditError(
            "Genesis only understands a single positional argument here "
            "(the neuron_id, or from_source's source)",
        )
    if decl.get("source") == source and decl.get("form") == form:
        return text

    callee = axon_callee(source, form)
    fields = _carry_over(decl, positional, source, callee, _gp)
    if form == "explicit":
        fields = _ensure_neuron_fn(fields, model)

    args = [_py_str(source)] if form == "from_source" else []
    if decl["scope"] == "factory":
        rendered = _render_stmt(decl.get("prefix", "return "), callee, fields,
                                decl["indent"], args)
    else:
        rendered = _render_call(decl["target"], callee, fields, args)
    return _validate(_replace_span(text, decl["lineno"], decl["end_lineno"], rendered))


def _carry_over(decl: dict[str, Any], positional: list[str], source: str,
                callee: str, _gp: Any) -> list[dict[str, Any]]:
    """Keywords that survive the conversion, in the order the file had them.

    A positional neuron_id is promoted to a keyword on the way through: the
    destination may be ``Axon.from_source``, whose only positional slot is
    the source itself, so leaving it positional would silently rename it.

    Provider keywords survive a *form* change (paired <-> from_source is the
    same Neuron written differently) and are dropped by a *provider* change,
    because ``model="gpt-4o"`` carried onto Ollama is a call that imports and
    then fails at the first TASK.
    """
    allowed = {f["name"] for f in _gp.declaration_fields(callee, source)}
    if decl.get("source") != source:
        allowed -= _gp.axon_provider_kwargs(source)
    kept = [dict(f) for f in decl["fields"]
            if f["name"] in allowed and f["name"] not in _EXPLICIT_ONLY]
    if any(f["name"] == "neuron_id" for f in kept) or not positional:
        return kept
    if decl["callee"] in AXON_SOURCE_CALLEES or decl["callee"] == "Axon":
        try:
            value = ast.literal_eval(positional[0])
        except (ValueError, SyntaxError):
            value = None
        if isinstance(value, str):
            return [{"name": "neuron_id", "type": "string", "value": value}, *kept]
    return kept


def _ensure_neuron_fn(fields: list[dict[str, Any]],
                      model: dict[str, Any]) -> list[dict[str, Any]]:
    """The explicit form needs a function to wrap, and won't invent one.

    Converting away from a provider leaves nothing to point ``neuron_fn`` at.
    A module-level async function that isn't already a decorated behaviour is
    the only honest candidate; with none, the conversion is refused so the
    user writes the Neuron first rather than getting a module that doesn't
    import.
    """
    if any(f["name"] == "neuron_fn" for f in fields):
        return fields
    taken = {b["fn_name"] for b in model["behaviors"]}
    candidates = [n for n in model["async_fns"] if n not in taken]
    if not candidates:
        raise EditError(
            "a hand-written Neuron needs an async function in this module for "
            "neuron_fn to point at - add one (async def f(input, context) -> "
            "dict) and switch again",
        )
    return [fields[0], {"name": "neuron_fn", "type": "name",
                        "value": candidates[0]}, *fields[1:]] \
        if fields else [{"name": "neuron_fn", "type": "name",
                         "value": candidates[0]}]
