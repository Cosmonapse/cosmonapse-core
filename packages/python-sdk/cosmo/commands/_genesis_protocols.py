"""
cosmo.commands._genesis_protocols
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
What a component can be asked to *do* - the catalogue behind Genesis's "add
behaviour" button.

Cosmonapse is a decorator/emitter model at the bare bones: a component
declares an identity, and every other thing it does is a decorated function.
So "what can this node do?" has a precise answer - the set of decorators the
object in that module actually carries - and this module is that answer,
split two ways:

**Own protocols** are the component's own decorators, and they depend on
what the module declared. An Axon has ``before_task`` and the ``detects_*``
family; a served Effector has ``on_tool_call``; a served Engram has
``on_recall`` / ``on_imprint`` / ``serves``. A *prebuilt* Engram
(``InMemoryEngram`` and friends) has none of them - it is finished storage,
not a hook surface - which is exactly why the Code tab lets you switch its
shape.

**Host protocols** are the Dendrite signal handlers any component can defer
onto its host with ``@X.host.on_<signal>``. That family is read live off
``Dendrite`` rather than hardcoded, so a signal added to the SDK shows up in
Genesis without anyone editing this file; the table below only supplies the
grouping and the one-liners.
"""

from __future__ import annotations

import inspect
from typing import Any

# One-way: _genesis_ast owns what the callees ARE (it has to recognise and
# rewrite them); this module owns what each one's form should offer.
from cosmo.commands._genesis_ast import AXON_SOURCE_CALLEES

# Read live off the SDK rather than hardcoded, for the same reason the host
# protocol table is: a dialect or a preset server added to cosmonapse should
# show up in Genesis's form without anyone editing this file.
from cosmonapse.effector.standards import TOOL_STANDARDS as _TOOL_STANDARDS
from cosmonapse.neuron import STANDARD_MCP_SERVERS as _MCP_SERVERS

# ---------------------------------------------------------------------------
# Own protocols
# ---------------------------------------------------------------------------
# handler_args is the signature Genesis writes for a fresh handler, and body
# is its starter - both chosen so the new block is *runnable* the moment it's
# created rather than a stub that breaks the module.

_LIFECYCLE: list[dict[str, Any]] = [
    {
        "name": "on_connect",
        "label": "on_connect",
        "blurb": "Fires once after the hosting Dendrite has started this component.",
        "handler_args": "component",
        "body": "return None",
        "decorator_args": [],
    },
    {
        "name": "on_refresh",
        "label": "on_refresh",
        "blurb": "Fires whenever this component's observable state changes.",
        "handler_args": "component, event",
        "body": "return None",
        "decorator_args": [],
    },
    {
        "name": "on_schedule",
        "label": "on_schedule",
        "blurb": "A background loop that runs every N seconds until stop().",
        "handler_args": "component",
        "body": "return None",
        "decorator_args": [
            {"name": "every_s", "type": "number", "value": 10, "required": True},
        ],
    },
]

_AXON_OWN: list[dict[str, Any]] = [
    {
        "name": "before_task",
        "label": "before_task",
        "blurb": "Transform, validate or reject the TASK input before the Neuron runs.",
        "handler_args": "input",
        "body": "return input",
        "decorator_args": [],
    },
    {
        "name": "detects_output",
        "label": "detects_output",
        "blurb": "Recognise the Neuron's raw output as a finished AGENT_OUTPUT payload.",
        "handler_args": "raw",
        "body": "return None",
        "decorator_args": [],
    },
    {
        "name": "detects_clarification",
        "label": "detects_clarification",
        "blurb": "Recognise output that is really a question: return {\"question\", \"context\"}.",
        "handler_args": "raw",
        "body": "return None",
        "decorator_args": [],
    },
    {
        "name": "detects_permission",
        "label": "detects_permission",
        "blurb": "Recognise output asking to act: return {\"action\", \"scope\", \"reason\"}.",
        "handler_args": "raw",
        "body": "return None",
        "decorator_args": [],
    },
    {
        "name": "detects_error",
        "label": "detects_error",
        "blurb": "Recognise a failure in the output: return {\"code\", \"message\", \"recoverable\"}.",
        "handler_args": "raw",
        "body": "return None",
        "decorator_args": [],
    },
]

_EFFECTOR_OWN: list[dict[str, Any]] = [
    {
        "name": "on_tool_call",
        "label": "on_tool_call",
        "blurb": "A TOOL_CALL arrives; your return value is emitted as the TOOL_RESULT. "
                 "Return None to fall through to the next handler.",
        "handler_args": "tool: str, args: dict",
        "body": 'if tool == "ping":\n    return {"pong": args}\nreturn None',
        "decorator_args": [],
    },
]

_ENGRAM_OWN: list[dict[str, Any]] = [
    {
        "name": "on_recall",
        "label": "on_recall",
        "blurb": "A RECALL arrives; return a list of Hit (or {\"id\", \"entry\", \"score\"} "
                 "dicts) as the RECALLED hits, or None to fall through.",
        "handler_args": "query, **kw",
        "body": "return None",
        "decorator_args": [],
    },
    {
        "name": "on_imprint",
        "label": "on_imprint",
        "blurb": "An IMPRINT arrives; return an ImprintReceipt or the new entry id "
                 "as the IMPRINTED receipt, or None to fall through.",
        "handler_args": "op, entry, **kw",
        "body": "return None",
        "decorator_args": [],
    },
    {
        "name": "serves",
        "label": "serves",
        "blurb": "The can_serve(query) -> bool gate. Optional; without it this Engram "
                 "answers every query once a recall handler exists.",
        "handler_args": "query",
        "body": "return True",
        "decorator_args": [],
    },
]

# A Receptor is caller-side: it originates TASKs and services no signal type.
# So it has no host decorators at all (see catalogue()), and its own hooks are
# about shaping a turn - what goes in, what comes back, what a failure looks
# like - rather than about answering the bus.
#
# on_signal is deliberately absent. It takes *signal_types varargs, which
# cannot be written as a keyword, so Genesis can neither parse nor render it;
# a hand-written one round-trips verbatim under "other".
_RECEPTOR_OWN: list[dict[str, Any]] = [
    {
        "name": "on_input",
        "label": "on_input",
        "blurb": "Transport payload -> the TASK input dict. Replaces the default "
                 "{input_key: text} wrapping entirely.",
        "handler_args": "raw",
        "body": 'return {"prompt": raw}',
        "decorator_args": [],
    },
    {
        "name": "on_result",
        "label": "on_result",
        "blurb": "Terminal Signal -> what the transport hands back. Default: raise on "
                 "ERROR, else payload[\"output\"].",
        "handler_args": "sig",
        "body": 'return sig.payload["output"]',
        "decorator_args": [],
    },
    {
        "name": "on_failure",
        "label": "on_failure",
        "blurb": "Exception -> a transport-shaped error value. Sees terminal ERRORs, "
                 "deadlines, and anything on_input / on_result raised. Returning a "
                 "value swallows it; re-raise to propagate.",
        "handler_args": "exc",
        "body": 'return {"error": str(exc)}',
        "decorator_args": [],
    },
]

#: CliReceptor only. A command function *returns the TASK input* - that is the
#: whole contract; the argparse tree and the REPL are derived from its
#: signature (no default -> positional, default -> --flag, bool -> store_true).
_CLI_OWN: list[dict[str, Any]] = [
    {
        "name": "command",
        "label": "command",
        "blurb": "A typed command. Its parameters become the CLI surface; its return "
                 "value is the TASK input. local=True answers here without dispatching.",
        "handler_args": 'name: str = "world"',
        "body": 'return {"name": name}',
        "decorator_args": [
            {"name": "name", "type": "string", "value": "", "required": False},
            {"name": "help", "type": "string", "value": "", "required": False},
            {"name": "local", "type": "bool", "value": False, "required": False},
            {"name": "default", "type": "bool", "value": False, "required": False},
        ],
    },
    {
        "name": "on_print",
        "label": "on_print",
        "blurb": "How a rendered result is written to the terminal. Sync or async.",
        "handler_args": "result",
        "body": "print(result)",
        "decorator_args": [],
    },
]

#: ApiReceptor and ChatReceptor only - an ordinary FastAPI route mounted on
#: this Receptor's router, alongside its dispatch endpoint.
_HTTP_OWN: list[dict[str, Any]] = [
    {
        "name": "route",
        "label": "route",
        "blurb": "An ordinary FastAPI route on this Receptor's router - the GET /memory "
                 "or GET /stats every deployment grows.",
        "handler_args": "",
        "body": 'return {"ok": True}',
        "decorator_args": [
            {"name": "path", "type": "string", "value": "/status", "required": True},
            {"name": "methods", "type": "string_list", "value": ["GET"], "required": False},
        ],
    },
]


def _own_for(kind: str, shape: str) -> list[dict[str, Any]]:
    if shape == "custom":
        # The component comes from a class this project defines. Which hooks it
        # carries depends on what that class inherits, which isn't knowable
        # from this module alone - so offer only the host decorators, which
        # every component has because they live on the base classes.
        return []
    if kind == "neuron":
        return _AXON_OWN + _LIFECYCLE
    if kind == "effector":
        return _EFFECTOR_OWN + _LIFECYCLE if shape == "served" else []
    if kind == "engram":
        if shape in ("served", "served-over-backend"):
            return _ENGRAM_OWN + _LIFECYCLE
        return []          # prebuilt: host protocols only
    if kind == "receptor":
        # No _LIFECYCLE: on_connect / on_refresh / on_schedule belong to
        # components the Dendrite hosts and drives. A Receptor is driven from
        # outside - run() owns its loop - so it has none of them.
        extra = _CLI_OWN if shape == "cli" else _HTTP_OWN if shape in ("api", "chat") else []
        return _RECEPTOR_OWN + extra
    return []


# ---------------------------------------------------------------------------
# Host protocols
# ---------------------------------------------------------------------------

#: Signal families, in the order the panel shows them. Names not listed here
#: still appear (under "Other signals") - the list is presentation, not a
#: whitelist.
_HOST_GROUPS: list[tuple[str, list[str]]] = [
    ("Task flow", [
        "on_task_signal", "on_agent_output", "on_final", "on_plan",
        "on_thought_delta", "on_critique", "on_error_signal",
    ]),
    ("Asking the human", [
        "on_clarification", "on_clarification_answer",
        "on_permission", "on_permission_decision", "on_escalation",
    ]),
    ("Tools", ["on_tool_call", "on_tool_result"]),
    ("Memory", [
        "on_recall_signal", "on_recalled", "on_imprint_signal",
        "on_imprinted", "on_memory_append", "on_context_sync",
    ]),
    ("Coordination", [
        "on_task_offer", "on_bid", "on_task_awarded", "on_task_declined",
        "on_consensus",
    ]),
    ("Presence", [
        "on_register_signal", "on_deregister_signal", "on_heartbeat_signal",
    ]),
]

_HOST_BLURBS: dict[str, str] = {
    "on_task_signal": "A TASK was dispatched on the bus.",
    "on_agent_output": "A Neuron produced output - the usual place to chain one into the next.",
    "on_final": "A workflow reached its conclusion.",
    "on_plan": "A Neuron published its plan.",
    "on_thought_delta": "A streaming chunk of a Neuron's reasoning.",
    "on_critique": "A critique of another participant's output.",
    "on_error_signal": "Something failed somewhere on the bus.",
    "on_clarification": "A Neuron is asking a question it needs answered.",
    "on_clarification_answer": "The answer to a CLARIFICATION came back.",
    "on_permission": "A Neuron is asking to be allowed to act.",
    "on_permission_decision": "A PERMISSION request was granted or denied.",
    "on_escalation": "Something was escalated for human attention.",
    "on_tool_call": "A tool was requested - serve it here to act as a tool host.",
    "on_tool_result": "A tool answered.",
    "on_recall_signal": "Someone is reading from memory.",
    "on_recalled": "A RECALL came back with hits.",
    "on_imprint_signal": "Someone is writing to memory.",
    "on_imprinted": "An IMPRINT was acknowledged.",
    "on_memory_append": "An entry was appended to shared context.",
    "on_context_sync": "Shared context was synchronised.",
    "on_task_offer": "A task was offered for bidding - evaluate it and bid to compete.",
    "on_bid": "A participant bid for an offered task.",
    "on_task_awarded": "A task was awarded to a participant.",
    "on_task_declined": "A participant declined a task.",
    "on_consensus": "A consensus round concluded.",
    "on_register_signal": "A participant joined the synapse.",
    "on_deregister_signal": "A participant left.",
    "on_heartbeat_signal": "A participant reported itself alive.",
}

#: The filters every Dendrite ``on_<signal>`` decorator accepts. Rendered as
#: the behaviour's optional narrowing fields.
_HOST_FILTERS = [
    {"name": "neuron", "type": "string", "value": "", "required": False,
     "blurb": "Only signals from this neuron id"},
    {"name": "capability", "type": "string", "value": "", "required": False,
     "blurb": "Only signals carrying this capability"},
    {"name": "trace_id", "type": "string", "value": "", "required": False,
     "blurb": "Only signals on this trace"},
]


def _host_specs() -> dict[str, list[str]]:
    """Every ``on_*`` a host proxy accepts, mapped to the filters it takes.

    Read off Dendrite so this can't drift from the SDK, and filtered by the
    *actual signature* rather than by name. Two things fall out of that which
    a hardcoded list would get wrong:

      * the deprecated aliases (``on_error`` -> ``on_error_signal`` and
        friends) take only ``fn`` and would warn on use, so they're excluded;
      * ``on_task_offer`` takes ``capability`` / ``trace_id`` but *not*
        ``neuron``, so its card offers exactly those two.

    Falls back to the curated list with the standard filters if cosmonapse
    isn't importable - a smaller menu beats a broken tab.
    """
    try:
        from cosmonapse.axon import _HostProxy
        from cosmonapse.dendrite import Dendrite
    except Exception:
        return {n: ["neuron", "capability", "trace_id"]
                for _, names in _HOST_GROUPS for n in names}

    unsupported = getattr(_HostProxy, "_UNSUPPORTED", frozenset())
    known = {f["name"] for f in _HOST_FILTERS}
    out: dict[str, list[str]] = {}
    for name in dir(Dendrite):
        if not name.startswith("on_") or name in unsupported:
            continue
        if _HostProxy._signal_type_for(name) is None:
            continue
        member = getattr(Dendrite, name, None)
        if not callable(member):
            continue
        try:
            params = inspect.signature(member).parameters
        except (ValueError, TypeError):
            continue
        filters = [
            p for p, v in params.items()
            if p in known and v.kind is inspect.Parameter.KEYWORD_ONLY
        ]
        if not filters:
            continue          # a lifecycle alias, not a filterable signal
        out[name] = filters
    return out


def host_protocols() -> list[dict[str, Any]]:
    """The host catalogue, grouped for the picker."""
    specs = _host_specs()
    available = set(specs)
    groups: list[dict[str, Any]] = []
    placed: set[str] = set()

    for title, names in _HOST_GROUPS:
        items = [_host_entry(n, specs[n]) for n in names if n in available]
        placed.update(n for n in names if n in available)
        if items:
            groups.append({"title": title, "protocols": items})

    rest = sorted(available - placed)
    if rest:
        groups.append({
            "title": "Other signals",
            "protocols": [_host_entry(n, specs[n]) for n in rest],
        })
    return groups


def _host_entry(name: str, filters: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "label": name,
        "blurb": _HOST_BLURBS.get(name, "A Dendrite signal handler."),
        "handler_args": "sig",
        "body": "return None",
        "decorator_args": [dict(f) for f in _HOST_FILTERS if f["name"] in filters],
    }


def _own_empty_reason(kind: str, shape: str) -> str | None:
    """Why this component has no decorators of its own."""
    if kind == "receptor" and shape == "custom":
        # The generic "custom" text below points at the host protocols, which
        # a Receptor does not have - so it needs its own wording.
        return (
            "This Receptor is built from a class defined in your project, so "
            "Genesis can't tell which hooks it carries. Receptors have no host "
            "protocols either - they originate signals rather than answering "
            "them - so any behaviour belongs in the class itself."
        )
    if shape == "custom":
        return (
            "This component is built from a class defined in your project, so "
            "Genesis can't tell which hooks it carries - that depends on what "
            "the class inherits. Host protocols work on any component and are "
            "listed below; anything else belongs in the class itself."
        )
    if kind == "engram":
        return (
            "A prebuilt backend is finished storage - it implements recall() and "
            "imprint() as methods, so there are no hooks to add. Switch the shape "
            "to a served Engram to get on_recall / on_imprint / serves."
        )
    return None


def catalogue(kind: str, shape: str, callee: str = "",
              source: str = "", backend_callee: str = "") -> dict[str, Any]:
    """The full "what can this node do" answer for one component.

    ``source`` is the Neuron provider behind an Axon, which the declaration
    carries because ``Axon.from_source`` names it positionally. It selects
    the provider kwargs the config form offers; it has no bearing on which
    decorators the component can host.

    ``backend_callee`` is the delegated storage a served Engram forwards to.
    It takes its own keywords - ``PostgresEngram`` needs a ``dsn`` that
    ``Engram.serve`` has never heard of - so the storage form gets its own
    table rather than being shown the declaration's.
    """
    own = _own_for(kind, shape)
    return {
        "kind": kind,
        "shape": shape,
        "declaration_fields": declaration_fields(callee, source),
        "backend_fields": declaration_fields(backend_callee) if backend_callee else [],
        "own": [{"title": "This component", "protocols": own}] if own else [],
        # A Receptor subscribes to nothing. The host decorators exist to
        # service signals a Dendrite routes to a component; a Receptor is
        # caller-side and is routed nothing, so offering them would be
        # offering hooks that could never fire.
        "host": [] if kind == "receptor" else host_protocols(),
        "own_empty_reason": _own_empty_reason(kind, shape) if not own else None,
    }


# ---------------------------------------------------------------------------
# Declaration fields
# ---------------------------------------------------------------------------
# What the config form should offer for each constructor, so a field the file
# doesn't currently set can still be added from the UI with a label and a
# sensible type instead of being invisible until someone types it by hand.
#
# ``expr`` fields are shown read-only: an EngramBinding list or a custom
# context_fetcher is a Python expression, and a text input that pretended
# otherwise would round-trip it into a string literal.

_F = dict


def _field(name, type_, blurb, *, required=False, suggest=None, placeholder="",
           secret=False):
    """One row of a config form.

    ``secret`` marks a value that shouldn't be legible on screen - the form
    masks it and offers a reveal toggle. It is a property of the keyword, not
    of the widget, so it is declared here next to the type rather than
    guessed from the name in the UI.
    """
    return {
        "name": name, "type": type_, "blurb": blurb, "required": required,
        "suggest": suggest or [], "placeholder": placeholder, "secret": secret,
    }

# --- Axon -----------------------------------------------------------------
# An Axon has three build forms and they take DIFFERENT keywords, so they get
# different tables. Sharing one table asked every source-paired call for a
# neuron_fn it refuses to accept, and hid the provider kwargs it does take.
#
#   Axon(neuron_id=, neuron_fn=, ...)          explicit - you supply the fn
#   Axon.from_source("groq", neuron_id=, ...)  any registered source
#   Axon.ollama(neuron_id=, model=, ...)       sugar for the above

_AXON_IDENTITY = [
    _field("neuron_id", "string", "This Neuron's identity on the bus - what a TASK is addressed to.",
           required=True, placeholder="summarize-notes"),
    _field("capabilities", "string_list", "What this Neuron claims it can do; dispatch can route on it.",
           placeholder="greet"),
    _field("version", "string", "Published on REGISTER.", placeholder="0.0.1"),
    _field("neuron_kind", "string", "The participant kind carried as directed.type.",
           suggest=["neuron"]),
]

_AXON_WIRING = [
    _field("tool_standard", "string",
           "The native tool-call dialect this model emits, so the Axon can recognise a "
           "call in its raw output. Required whenever effectors= is set.",
           suggest=list(_TOOL_STANDARDS)),
    _field("context_fetcher", "expr", "Async callable resolving a context_ref into history."),
    _field("engrams", "expr", "EngramBinding list - the memories this Neuron may address."),
    _field("effectors", "expr",
           "EffectorBinding list - the tools this Neuron may call. Refused at construction "
           "without tool_standard, because the binding would be dead wiring."),
]

#: Only the source-paired forms have these: from_source builds the Neuron, so
#: it also decides what parses its output and what the model is told.
_AXON_RECOGNITION = [
    _field("recognize", "bool",
           "Attach this source's recogniser, so a {\"cosmo\": ...} block in the reply "
           "becomes a real CLARIFICATION / PERMISSION / ERROR Signal. On by default."),
    _field("teach_intents", "bool",
           "Append COSMO_INTENT_SYSTEM_PROMPT to system= so the model knows that "
           "convention. Defaults on when recognize is set and the source takes system=."),
]

_AXON_EXPLICIT = [
    _AXON_IDENTITY[0],
    _field("neuron_fn", "name", "The async function this Axon wraps. Defined in this module.",
           required=True),
] + _AXON_IDENTITY[1:] + _AXON_WIRING + [
    _field("output_parser", "expr", "Normalises this Neuron's native output before wrapping."),
]


def _api_key(env: str) -> dict[str, Any]:
    return _field("api_key", "string", f"Bearer token. Falls back to ${env}.",
                  secret=True)


_TEMP = _field("temperature", "number", "Sampling temperature.")
_TIMEOUT = _field("timeout", "number", "HTTP timeout in seconds. Defaults to 120.")

#: An OpenAI-compatible hosted provider is a pre-configured HuggingFace
#: Neuron, so it takes that wrapper's kwargs with the base URL already set.
_OPENAI_COMPATIBLE = [
    _field("model", "string", "Model name forwarded in the request body.", required=True),
    _api_key(""),
    _field("endpoint", "string", "Override the provider's base URL."),
    _TEMP,
    _field("max_new_tokens", "number", "Maximum tokens to generate. Defaults to 512."),
    _field("use_chat_api", "bool", "Use /v1/chat/completions. Pre-set for this provider."),
    _TIMEOUT,
]


def _hosted(env: str) -> list[dict[str, Any]]:
    return [dict(f, **({"blurb": f"Bearer token. Falls back to ${env}."}
                       if f["name"] == "api_key" else {}))
            for f in _OPENAI_COMPATIBLE]


#: Provider -> the kwargs its Neuron wrapper accepts (cosmonapse/neuron.py).
AXON_SOURCE_FIELDS: dict[str, list[dict[str, Any]]] = {
    "ollama": [
        _field("model", "string", "Ollama model tag.", required=True,
               suggest=["llama3", "mistral", "phi3", "qwen2.5-coder"]),
        _field("endpoint", "string", "Base URL of the Ollama daemon.",
               placeholder="http://localhost:11434"),
        _field("system", "string", "System prompt injected before any user message."),
        _TEMP,
        _field("max_tokens", "number", "Maximum tokens to generate (num_predict)."),
        _TIMEOUT,
    ],
    "huggingface": [
        _field("endpoint", "string",
               "Base URL of the inference server - TGI, vLLM, LM Studio, llama.cpp, "
               "or a hosted HF endpoint.", required=True,
               placeholder="http://localhost:8080"),
        _field("model", "string", "Model name, required by multi-model servers like vLLM."),
        _field("use_chat_api", "bool", "Force the /v1/chat/completions path."),
        _field("use_completions_api", "bool",
               "Use the raw /v1/completions path. Mutually exclusive with use_chat_api; "
               "render the chat template yourself and pass prompt=."),
        _field("stop", "string_list", "Stop sequences, sent on every path."),
        _TEMP,
        _field("max_new_tokens", "number", "Maximum tokens to generate. Defaults to 512."),
        _api_key("HF_TOKEN"),
        _TIMEOUT,
    ],
    "openai": [
        _field("model", "string", "Model name.", required=True,
               suggest=["gpt-4o", "gpt-4o-mini"]),
        _api_key("OPENAI_API_KEY"),
        _field("endpoint", "string", "Override the API base URL.",
               placeholder="https://api.openai.com/v1"),
        _TEMP,
        _field("max_tokens", "number", "Maximum tokens to generate."),
        _field("system", "string", "System prompt injected before any user message."),
        _TIMEOUT,
    ],
    "anthropic": [
        _field("model", "string", "Model name.", required=True,
               suggest=["claude-opus-4-6", "claude-sonnet-4-5"]),
        _api_key("ANTHROPIC_API_KEY"),
        _field("system", "string", "System prompt, sent as the top-level system field."),
        _field("max_tokens", "number", "Maximum tokens to generate. Required by the API."),
        _TEMP,
        _TIMEOUT,
    ],
    "groq": _hosted("GROQ_API_KEY"),
    "openrouter": _hosted("OPENROUTER_API_KEY"),
    "together": _hosted("TOGETHER_API_KEY"),
    "mistral": _hosted("MISTRAL_API_KEY"),
    "mcp": [
        _field("server", "string", "Preset server name from STANDARD_MCP_SERVERS.",
               suggest=list(_MCP_SERVERS)),
        _field("command", "string", "Executable to launch, when there's no preset.",
               placeholder="npx"),
        _field("args", "string_list", "Arguments appended to the launch command."),
        _field("env", "expr", "Extra environment for the subprocess."),
        _field("cwd", "string", "Working directory for the subprocess."),
        _field("tool", "string", "Pin every call to one tool name."),
    ],
}

def axon_provider_kwargs(source: str) -> set[str]:
    """Keyword names that belong to one provider rather than to the Axon.

    A conversion has to tell them apart: ``capabilities`` means the same
    thing whatever builds the Neuron, but ``model``, ``endpoint`` and
    ``api_key`` are answers to a question the old provider was asked. They
    share names across providers, which is exactly why carrying them by name
    would produce a call that imports fine and points at nothing.
    """
    return {f["name"] for f in AXON_SOURCE_FIELDS.get(source, [])}


def _paired_fields(source: str) -> list[dict[str, Any]]:
    """Identity + wiring + recognition + the provider's own kwargs.

    ``mcp`` is never taught the cosmo intent convention (its wrapper takes no
    ``system=``) and its recogniser maps ``is_error`` rather than parsing
    text, so ``teach_intents`` is left off its form entirely.
    """
    recognition = _AXON_RECOGNITION if source != "mcp" else _AXON_RECOGNITION[:1]
    return (_AXON_IDENTITY + _AXON_WIRING + recognition
            + AXON_SOURCE_FIELDS.get(source, []))


_ENGRAM_COMMON = [
    _field("engram_id", "string", "How this memory is addressed in RECALL / IMPRINT.",
           required=True, placeholder="session-memory"),
    _field("engram_kind", "string", "Routing label. No semantics enforced - a deployment convention.",
           suggest=["context", "semantic", "keyvalue", "relational", "blob"]),
    _field("capabilities", "string_list", "Free-form claims about what this backend can do.",
           suggest=["substring", "tags", "merge_key", "time_range"]),
    _field("version", "string", "Published on REGISTER.", placeholder="0.0.1"),
]


# --- Receptor -------------------------------------------------------------
# The dispatch trio is shared by all three: every Receptor turns a payload
# into the same TASK an orchestrator Dendrite always emitted. What differs is
# the transport, so the per-class tables below add only their own deployment
# keywords on top of this.

_RECEPTOR_COMMON = [
    _field("neuron", "string",
           "The Neuron a turn is addressed to. Either this or capabilities= is "
           "needed, here or per call.", placeholder="hello"),
    _field("capabilities", "string_list",
           "Route by claim instead of by name - the alternative to neuron=.",
           placeholder="greet"),
    _field("receptor_id", "string",
           "Stamped on meta.receptor of every TASK this raises, which is how the "
           "signal view attributes traffic back to this edge."),
    _field("input_key", "string",
           "The key the raw payload is wrapped under to form the TASK input.",
           suggest=["prompt", "message", "goal", "text"]),
    _field("timeout_s", "number", "Deadline for a turn, in seconds. Defaults to 60."),
    _field("scope", "string", "Dispatch scope passed through to the Dendrite.",
           suggest=["all", "any"]),
    _field("finalize", "bool",
           "Whether a plain worker trace should be closed on AGENT_OUTPUT. Without "
           "it a trace that never emits FINAL would hang to the deadline."),
    _field("meta", "expr", "Extra meta merged into every TASK this Receptor raises."),
]

_MODE = _field("mode", "string",
               "Default dispatch mode. send returns immediately, wait blocks for the "
               "terminal Signal, stream yields them as they arrive.",
               suggest=["send", "wait", "stream"])

_HTTP_DEPLOY = [
    _field("host", "string", "Interface run() binds.", placeholder="127.0.0.1"),
    _field("port", "number", "Port run() binds. Two HTTP Receptors sharing a "
                             "(host, port) are merged onto one app.", placeholder="8000"),
]


DECLARATION_FIELDS: dict[str, list[dict[str, Any]]] = {
    "Axon": _AXON_EXPLICIT,
    "Effector.serve": [
        _field("effector_id", "string", "How this tool host is addressed in a TOOL_CALL.",
               required=True, placeholder="http-tools"),
        _field("effector_kind", "string", "Routing label for the tool family.",
               suggest=["tools", "filesystem", "http", "shell", "mcp"]),
        _field("version", "string", "Published on REGISTER.", placeholder="0.0.1"),
    ],
    "Engram.serve": list(_ENGRAM_COMMON),
    "InMemoryEngram": list(_ENGRAM_COMMON),
    "SqliteEngram": [
        _field("path", "string", "Database file. ':memory:' keeps it in-process.",
               placeholder=":memory:"),
    ] + _ENGRAM_COMMON,
    "PostgresEngram": [
        _field("dsn", "string", "Connection string.", required=True,
               placeholder="postgresql://localhost/cosmonapse"),
    ] + _ENGRAM_COMMON + [
        _field("min_size", "number", "Minimum pool size."),
        _field("max_size", "number", "Maximum pool size."),
        _field("pool_kwargs", "expr", "Extra kwargs passed to the pool."),
    ],
    "CliReceptor": [
        _field("prog", "string", "Program name in --help, and the default receptor_id.",
               placeholder="myproject"),
        _field("description", "string", "One line under the usage in --help."),
        _MODE,
    ] + _RECEPTOR_COMMON + [
        _field("banner", "string", "Printed once when the REPL starts."),
        _field("prompt", "string", "The REPL prompt.", placeholder="> "),
    ],
    "ApiReceptor": [
        _field("path", "string", "Route the dispatch endpoint is mounted at.",
               placeholder="/dispatch"),
        _MODE,
    ] + _RECEPTOR_COMMON + [
        _field("allowed_modes", "expr",
               "The set of modes a request body may ask for. Defaults to all three."),
        _field("max_timeout_s", "number",
               "Ceiling on the deadline a request may ask for. Defaults to 600."),
    ] + _HTTP_DEPLOY,
    "ChatReceptor": [
        _field("path", "string", "Route the chat page and its endpoint are mounted at.",
               placeholder="/chat"),
        _field("title", "string", "Title shown on the served page."),
        _field("greeting", "string", "First line the page shows.",
               placeholder="Ask me something."),
        _field("voice", "bool",
               "Enable speech in the served page. Client-side only - the Web Speech "
               "API in the browser. No audio crosses the wire."),
        _field("history_turns", "number",
               "Prior turns carried per session. 0 makes every turn independent."),
        _MODE,
    ] + _RECEPTOR_COMMON + _HTTP_DEPLOY,
}


def declaration_fields(callee: str, source: str = "") -> list[dict[str, Any]]:
    """The known constructor keywords for a callee, for the config form.

    ``Axon(...)`` and ``Axon.ollama(...)`` are *different forms* and were
    sharing one table, which asked for ``neuron_fn`` on a call that refuses
    it and hid every kwarg the paired form actually takes. A source-paired
    callee gets identity + wiring + recognition + that provider's own
    kwargs; ``source`` disambiguates ``Axon.from_source``, whose provider is
    the first positional argument rather than the method name.
    """
    if callee in AXON_SOURCE_CALLEES or callee == "Axon.from_source":
        src = AXON_SOURCE_CALLEES.get(callee) or source
        return [dict(f) for f in _paired_fields(src)]
    return [dict(f) for f in DECLARATION_FIELDS.get(callee, [])]
