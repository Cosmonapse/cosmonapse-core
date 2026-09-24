"""
cosmonapse.axon
~~~~~~~~~~~~~~~
Agent-side tool that turns a Neuron's raw output into a protocol-valid
Signal and hands it to its Dendrite.

The Axon does not touch the Synapse. It owns:
  - the Neuron's identity (neuron_id, capabilities, version)
  - the body of the tool (neuron_fn)
  - response validation: agent output -> AGENT_OUTPUT,
                         raised exception -> ERROR,
                         clarification marker -> CLARIFICATION

Host-side behaviour (the standard wiring pattern):
  @axon.host.on_<signal>    deferred Dendrite decorator - queued at module
                            level, registered on the HOSTING Dendrite once
                            it announces this Axon (subscription ensured).
                            e.g. @axon.host.on_agent_output(neuron="planner"),
                                 @axon.host.on_tool_call(neuron="websearch")

Lifecycle hooks (from cosmonapse._hooks.LifecycleHooks):
  @axon.on_connect          fires after the hosting Dendrite has emitted
                            REGISTER for this Axon (and after @host.on_*
                            registrations have been applied)
  @axon.on_refresh          fires on each heartbeat tick from the
                            hosting Dendrite (reason="heartbeat")
  @axon.on_schedule(every_s=N)  developer-supplied periodic task

Clarification convention
------------------------
If the agent returns a dict with `__clarification__: True`, the Axon
emits CLARIFICATION instead of AGENT_OUTPUT.
"""

from __future__ import annotations

import inspect
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from cosmonapse._hooks import LifecycleHooks, RefreshEvent
from cosmonapse.effector.base import (
    EffectorBinding,
    EffectorError,
    EffectorNotBound,
)
from cosmonapse.effector.schema import render_tools, validate_args
from cosmonapse.effector.standards import (
    TOOL_STANDARDS,
    extract_tool_calls,
)
from cosmonapse.engram.base import EngramBinding, EngramNotBound
from cosmonapse.envelope import (
    Directed,
    Signal,
    SignalType,
    agent_output_signal,
    clarification_signal,
    error_signal,
    permission_signal,
    trace_context,
)
from cosmonapse.glia import (
    AUDIT_DOMAIN,
    F_ATTEMPT,
    F_ATTEMPTS,
    F_HASH,
    META_KEY,
    AuditKind,
    AuditOutcome,
    CallerGate,
    Decision,
    Direction,
    Glia,
    Mode,
    PolicyOutcome,
    PolicyRefusal,
    RepairPolicy,
    Retry,
    allowed_retry,
    audit_outcome,
    caller_gate,
    canonical_text,
    is_exempt,
    mark_exempt,
    outcome_for,
    publish_audit,
    with_glia_record,
    with_payload,
)

if TYPE_CHECKING:
    from cosmonapse.dendrite import Dendrite

logger = logging.getLogger(__name__)


NeuronFn = Callable[[dict[str, Any], list[Any]], Awaitable[dict[str, Any]]]
ContextFetcher = Callable[[str], Awaitable[list[Any]]]
# An OutputParser recognises a Neuron's *native* output (an LLM's
# ``{"response": text}``, an MCP server's ``{"is_error", "content", ...}``)
# and normalises it into the marker dict the Axon already understands:
# ``__clarification__`` / ``__permission__`` / ``__error__`` markers, or a
# plain result dict. It is the per-source recognition the Axon applies before
# wrapping. Pure and synchronous; raising inside it yields an ERROR Signal.
OutputParser = Callable[[dict[str, Any]], dict[str, Any]]

# Deadline applied to a native tool call dispatched by the Axon when the
# matched EffectorBinding declares no default_deadline_ms of its own. A
# tool call must not hang the TASK forever.
DEFAULT_TOOL_DEADLINE_MS = 30_000


async def _noop_context_fetcher(ref: str) -> list[Any]:
    return []


def _jsonable(value: Any) -> Any:
    """Shape a pending output for a PERMISSION's ``context``. Signals are
    serialised by pydantic, so anything put on one has to survive
    ``model_dump_json``; a repr is a lossy but always-encodable fallback."""
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return repr(value)


class InvalidOutput(Exception):
    """A Neuron's output was not claimed by anything that could claim it.

    Raised only when the Axon was built with ``strict_output=True``.
    Without it, ``_apply_recognisers`` returns the raw output both when
    nothing matched AND when the output is a perfectly good plain answer,
    so an unclaimed output is not detectable and the repair loop has
    nothing to trigger on (GLIA_DESIGN section 8.2).

    An unrecognised tool call is the case worth the flag:
    ``extract_tool_calls`` returns ``[]`` on no match, the raw text falls
    through, and the model's attempted call becomes the AGENT_OUTPUT
    answer. Once a miss costs one more round of prompting, the text
    parsers can afford to be STRICTER rather than more permissive, which
    is what you want from something that must never misfire on ordinary
    JSON.
    """

    #: The output nothing claimed, carried so the handler can wrap it as
    #: the fallback reply without relying on loop-local bindings.
    output: Any = None


#: Which audit categories the correction loop can produce. Only these
#: three: a guard refusal, an output nothing claimed, and arguments that
#: failed validation. A tool that timed out or failed is NOT here - that
#: is transient territory, on the Effector and Engram side, and re-asking
#: the model about it is a different decision.
REPAIR_KINDS: frozenset[AuditKind] = frozenset({
    AuditKind.GUARD_RETRY, AuditKind.EVAL_RETRY, AuditKind.TOOL_RETRY,
})

#: Bound on how many traces one Axon remembers a repair count for.
_MAX_REPAIR_TRACES = 4096


def _raise_unclaimed(output: Any) -> None:
    """Raise :class:`InvalidOutput` carrying the output nothing claimed."""
    exc = InvalidOutput(
        "no recogniser, parser or tool dialect claimed this output"
    )
    exc.output = output
    raise exc


@dataclass
class _Repair:
    """A pass that should be re-asked, and the reply to send if it cannot
    be. ``fallback`` is exactly what this Axon would have returned before
    the loop existed, so exhausting the loop is never worse than not
    having it."""

    kind: AuditKind
    problem: str
    #: The reply to send when the loop cannot fix it. Optional only
    #: because the tool-call path learns it one frame up, where the
    #: observation has been wrapped into an AGENT_OUTPUT.
    fallback: Signal | None = None
    policy_id: str | None = None
    #: The gate scope a policy refusal came from: which way the signal was
    #: crossing, and its type. None for a repair no policy caused.
    direction: Direction | None = None
    signal_type: SignalType | None = None
    #: Digest of the matched content, so this record joins the
    #: component's local journal line. Never the content itself.
    digest: str | None = None
    #: The refusing policy's own retry budget. The card's
    #: ``repair.max_attempts`` is still the ceiling.
    max_attempts: int | None = None
    #: The refusal this repair answers, so the pass's gate does not record
    #: it a second time.
    refusal: PolicyRefusal | None = None

    @classmethod
    def from_refusal(
        cls, exc: PolicyRefusal, fallback: Signal | None = None,
    ) -> _Repair:
        oc = exc.outcome
        return cls(
            kind=AuditKind.GUARD_RETRY,
            problem=exc.reason,
            fallback=fallback,
            policy_id=oc.declared.policy_id,
            direction=oc.direction,
            signal_type=oc.signal_type,
            digest=oc.record.get(F_HASH),
            max_attempts=oc.max_attempts,
            refusal=exc,
        )

    def as_record(self, attempt: int, took_ms: int) -> dict[str, Any]:
        """One entry for ``meta.glia.attempts``: the attempt number,
        the audit category, the outcome and the duration. The policy id,
        never the model's rejected output.

        Deliberately the same words the AUDIT signal uses, so a reader
        holding either one is reading one vocabulary.
        """
        rec: dict[str, Any] = {
            "attempt": attempt,
            "kind": self.kind.value,
            "domain": AUDIT_DOMAIN[self.kind],
            # Overwritten by the loop when this was the last attempt.
            "outcome": AuditOutcome.RE_ASKED.value,
            "took_ms": took_ms,
        }
        if self.policy_id:
            rec["policy_id"] = self.policy_id
        if self.direction is not None:
            rec["direction"] = self.direction.value
        if self.signal_type is not None:
            rec["signal"] = self.signal_type.value
        return rec


class _ToolCallEscalation(Exception):
    """Internal: a policy on an outbound TOOL_CALL asked to escalate.

    Raised out of ``_run_tool_call`` because that method returns a tool
    OBSERVATION (a dict inside an AGENT_OUTPUT payload) and an escalation
    has to replace the whole reply with a PERMISSION. Caught in
    ``_dispatch_native_tool_calls``; never seen by a caller.
    """

    def __init__(self, outcome: PolicyOutcome, tool: str) -> None:
        self.outcome = outcome
        self.tool = tool
        super().__init__(outcome.reason or "escalated by policy")


class _HostProxy:
    """Deferred Dendrite signal decorators, declared on the Axon.

    ``@axon.host.on_<signal>(**filters)`` queues a handler registration at
    module level; the Axon replays it onto the **hosting Dendrite** right
    after that Dendrite emits REGISTER for this Axon (i.e. just before the
    ``@axon.on_connect`` hooks fire), and ensures the matching inbound
    subscription. This is THE standard way to declare host-side behaviour
    (chain handlers, tool servers) in a Neuron's module - no hand-written
    ``on_connect`` wiring::

        @AXON.host.on_agent_output(neuron="planner")
        async def chain(sig): ...

        @AXON.host.on_tool_call(neuron="websearch")
        async def call(sig): ...

    Any ``Dendrite.on_*`` signal decorator with the standard
    ``(fn, *, neuron=, capability=, trace_id=)`` shape is accepted; the
    name is validated eagerly so a typo fails at import time, not at
    connect time.
    """

    #: Dendrite ``on_*`` methods with a non-standard registration shape.
    _UNSUPPORTED: frozenset[str] = frozenset({"on_discover", "on_trace"})

    def __init__(self, axon: Axon) -> None:
        self._axon = axon

    @staticmethod
    def _signal_type_for(name: str) -> SignalType | None:
        key = name[3:].removesuffix("_signal").upper()
        try:
            return SignalType[key]
        except KeyError:
            return None

    def __getattr__(self, name: str) -> Any:
        if not name.startswith("on_") or name in self._UNSUPPORTED:
            raise AttributeError(
                f"axon.host has no decorator {name!r} - use the "
                f"Dendrite's on_<signal> family (e.g. on_agent_output, "
                f"on_tool_call)"
            )
        st = self._signal_type_for(name)
        from cosmonapse.dendrite import Dendrite
        if st is None or not hasattr(Dendrite, name):
            raise AttributeError(
                f"axon.host.{name}: not a Dendrite signal decorator"
            )

        def register(fn: Any = None, **filters: Any) -> Any:
            def deco(f: Any) -> Any:
                self._axon._host_regs.append((name, st, dict(filters), f))
                return f
            return deco(fn) if callable(fn) else deco
        return register


class Axon(LifecycleHooks):
    """Agent-side tool that turns raw Neuron output into protocol-valid Signals."""

    def __init__(
        self,
        *,
        neuron_id: str,
        neuron_fn: NeuronFn,
        capabilities: list[str] | None = None,
        catch_all: bool = False,
        version: str | None = None,
        neuron_kind: str = "neuron",
        context_fetcher: ContextFetcher | None = None,
        engrams: list[EngramBinding] | None = None,
        effectors: list[EffectorBinding] | None = None,
        tool_standard: str | None = None,
        parallel_tools: bool = False,
        output_parser: OutputParser | None = None,
        glia: Glia | None = None,
        strict_output: bool = False,
        repair: RepairPolicy | None = None,
    ) -> None:
        LifecycleHooks.__init__(self)
        self.neuron_id = neuron_id
        self.capabilities = capabilities or []
        # Answer TASKs that name neither a neuron nor any capability - the
        # open call (see Dendrite.dispatch_task). Off by default: an Axon
        # silently widening its own inbox is the kind of surprise that makes
        # a namespace hard to reason about, so it has to be asked for. It
        # changes nothing about addressed or capability-routed delivery.
        self.catch_all = catch_all
        self.version = version
        # The participant kind carried on REGISTER as ``directed.type`` -
        # the Neuron-side analogue of an Engram's ``engram_kind``. Defaults
        # to the generic ``"neuron"`` so every REGISTER has a typed directed.
        self.neuron_kind = neuron_kind
        self._fn = neuron_fn
        self._context_fetcher = context_fetcher or _noop_context_fetcher
        self._output_parser = output_parser
        self._dendrite: Dendrite | None = None

        # Deferred host-side registrations (@axon.host.on_<signal>), replayed
        # onto the hosting Dendrite when it announces this Axon.
        self._host_regs: list[tuple[str, Any, dict[str, Any], Any]] = []
        self._host_regs_applied = False

        # Decorator-registered recognisers, one bucket per capability. Each
        # entry is a detector (sync or async) that inspects the Neuron's raw
        # output and returns the intent's fields (dict) on a match, or None to
        # fall through. Applied in fixed precedence by ``_apply_recognisers``.
        self._recognisers: dict[str, list[Callable[[Any], Any]]] = {
            "error": [],
            "clarification": [],
            "permission": [],
            "output": [],
        }

        # Pre-task hooks (@axon.before_task): transform/validate/reject the
        # TASK input before the Neuron runs.
        self._before_task_hooks: list[Callable[[dict[str, Any]], Any]] = []

        # The Glia card, or None. The gate sits behind a null check on
        # this, so an Axon with no card does zero extra work -
        # the same shape as ``if self._before_task_hooks`` above and the
        # ``if not any(rec.values())`` early-out in _apply_recognisers.
        self._glia = glia
        # Strict recognition: an output no recogniser claimed raises
        # InvalidOutput instead of passing through as the answer, which
        # is what makes an unclaimed output detectable at all. Opt-in,
        # because passing it through is the behaviour every existing
        # brain is written against. See GLIA_DESIGN section 8.2.
        self._strict_output = bool(strict_output)
        # Correction repair. An explicit policy wins over the card's, and
        # with neither there is NO loop: the Axon behaves exactly as it
        # did before this existed.
        self._repair = repair
        # Builders for the next attempt's input (@axon.repairs).
        self._repair_builders: list[Callable[..., Any]] = []
        # Refusals and corrections seen per trace, as the attempt records
        # themselves so a failure path can carry what happened. Bounded:
        # an Axon that runs forever must not grow a row per trace it
        # ever saw.
        self._repair_counts: dict[str, list[dict[str, Any]]] = {}

        # Engram bindings the Neuron may address. Keyed by binding.name  -
        # the Neuron passes that name to recall(...) / imprint(...). The
        # Axon enforces the whitelist so a Neuron cannot hit an Engram it
        # was not declared to depend on.
        self._engram_bindings: dict[str, EngramBinding] = {}
        for b in (engrams or []):
            if b.name in self._engram_bindings:
                raise ValueError(
                    f"Axon {neuron_id!r}: duplicate EngramBinding name "
                    f"{b.name!r}"
                )
            self._engram_bindings[b.name] = b

        # Effector bindings - the tools the Neuron may act through. THE
        # RULE: an Axon may hold EffectorBindings only when tool calls
        # are enabled via tool_standard=, naming the native dialect its
        # Neuron emits ("hermes" | "claude" | "codex"). Without a
        # standard the Axon cannot recognise a call in the raw output,
        # so the bindings would be dead wiring - fail at construction,
        # not silently at runtime. tool_standard alone (no bindings) is
        # legal: pure translation, dispatch left to the host chain.
        #
        # An explicitly passed tool_standard ALWAYS wins. When it is
        # omitted the dialect is inferred from the wired Neuron (its
        # provider class, then its model name), so the caller does not
        # have to restate a fact the wiring already determines. Inference
        # only ever fires where construction used to raise, so no
        # existing Axon changes behaviour.
        self._tool_standard: str | None = None
        if tool_standard is not None:
            std = tool_standard.lower()
            if std not in TOOL_STANDARDS:
                raise ValueError(
                    f"Axon {neuron_id!r}: unknown tool_standard "
                    f"{tool_standard!r}; supported: "
                    f"{sorted(TOOL_STANDARDS)}"
                )
            self._tool_standard = std
        else:
            self._tool_standard = _infer_tool_standard(neuron_fn)
        if effectors and self._tool_standard is None:
            raise ValueError(
                f"Axon {neuron_id!r}: effectors= requires tool_standard= "
                f"(one of {sorted(TOOL_STANDARDS)}) so the Axon can "
                f"recognise the Neuron's native tool calls; it could not "
                f"be inferred from {type(neuron_fn).__name__}"
            )
        # Run every recognised tool call in a reply, in order, instead of
        # the first only. Off by default: the one-action-per-step
        # contract is what the existing host chains are written against,
        # and quietly running three tools where one used to run is not a
        # change an Axon should make on its own. When off, the calls that
        # did NOT run are reported on ``dropped_calls`` rather than
        # vanishing.
        self._parallel_tools = parallel_tools
        self._provider = _provider_of(neuron_fn)
        self._effector_bindings: dict[str, EffectorBinding] = {}
        for eb in (effectors or []):
            if eb.name in self._effector_bindings:
                raise ValueError(
                    f"Axon {neuron_id!r}: duplicate EffectorBinding name "
                    f"{eb.name!r}"
                )
            self._effector_bindings[eb.name] = eb

        # The provider-shaped tools= payload, rendered once from every
        # binding's declared schemas. None when nothing declares one, and
        # that None is the whole backward-compatibility guarantee: no
        # schemas means no native payload, no injected input key, and the
        # text-parsing path exactly as it was.
        _schemas = [
            s for b in self._effector_bindings.values() for s in b.schemas
        ]
        self._native_tools: list[dict[str, Any]] | None = (
            render_tools(_schemas, self._provider) if _schemas else None
        )
        # Recognition runs at all only when this Axon is about tools.
        self._tools_enabled = bool(
            self._tool_standard or self._native_tools,
        )

        # Whether the wrapped neuron_fn declares recall/imprint kwargs.
        # Detected once at construction; cached for hot-path use.
        self._fn_accepts_recall: bool = False
        self._fn_accepts_imprint: bool = False
        self._fn_accepts_call_tool: bool = False
        self._fn_accepts_kwargs: bool = False
        try:
            import inspect as _inspect
            sig = _inspect.signature(neuron_fn)
            for _pname, _p in sig.parameters.items():
                if _p.kind is _inspect.Parameter.VAR_KEYWORD:
                    self._fn_accepts_kwargs = True
                    self._fn_accepts_recall = True
                    self._fn_accepts_imprint = True
                    self._fn_accepts_call_tool = True
                    break
                if _pname == "recall":
                    self._fn_accepts_recall = True
                if _pname == "imprint":
                    self._fn_accepts_imprint = True
                if _pname == "call_tool":
                    self._fn_accepts_call_tool = True
        except (ValueError, TypeError):
            # Builtins / C functions have no inspectable signature. Skip
            # helper injection and fall back to the 2-arg legacy call.
            pass

        self._warn_repair_deadlines()

    def _warn_repair_deadlines(self) -> None:
        """Warn when repair attempts could outlast the caller's timeout.

        Inner attempts multiplied by their cost must stay under the
        caller's ``timeout_s``, and under ``DEFAULT_TOOL_DEADLINE_MS``
        per tool call within them. Exceed it and the OUTER retry
        (RetryStrategy) times out mid-repair, re-dispatches on a fresh
        trace, and STOPs an attempt that was about to succeed. Nothing
        checked this before, and the Axon cannot know ``timeout_s`` at
        construction - so it states the worst case and leaves the
        arithmetic to the caller (GLIA_DESIGN section 8.3).
        """
        repair = self.repair
        if repair is None or repair.max_attempts <= 0 or not self._tools_enabled:
            return
        per_call = max(
            [
                b.default_deadline_ms or DEFAULT_TOOL_DEADLINE_MS
                for b in self._effector_bindings.values()
            ] or [DEFAULT_TOOL_DEADLINE_MS]
        )
        worst_ms = per_call * (repair.max_attempts + 1)
        logger.warning(
            "Axon %s: repair is on (max_attempts=%d) and tool calls are "
            "enabled, so one TASK can take up to %.1fs of tool deadlines "
            "(%d attempts x %dms). Keep dispatch timeout_s above that, or "
            "the outer RetryStrategy will time out mid-repair, re-dispatch "
            "on a fresh trace and STOP an attempt that was about to "
            "succeed.",
            self.neuron_id, repair.max_attempts, worst_ms / 1000.0,
            repair.max_attempts + 1, per_call,
        )

    # -- source-paired factories --------------------------------------
    # An Axon wraps a Neuron. These build an Axon already paired with one
    # of the existing ``Neuron(source=...)`` providers AND wired with the
    # matching recogniser, so the Axon handles the protocol interactions
    # (output / clarification / permission / error) out of the box. No new
    # class: the result is a plain Axon.

    @classmethod
    def from_source(
        cls,
        source: str,
        *,
        neuron_id: str,
        capabilities: list[str] | None = None,
        version: str | None = None,
        neuron_kind: str = "neuron",
        context_fetcher: ContextFetcher | None = None,
        engrams: list[EngramBinding] | None = None,
        effectors: list[EffectorBinding] | None = None,
        tool_standard: str | None = None,
        parallel_tools: bool = False,
        recognize: bool = True,
        teach_intents: bool | None = None,
        glia: Glia | None = None,
        strict_output: bool = False,
        **source_kwargs: Any,
    ) -> Axon:
        """Build an Axon around ``Neuron(source=source, **source_kwargs)``.

        Works for every registered source (``ollama``, ``huggingface``/``hf``,
        ``openai``, ``anthropic``, ``groq``, ``openrouter``, ``together``,
        ``mistral``, ``mcp``). When ``recognize`` is True (default) the Axon is
        given the recogniser matching the source family: the MCP recogniser for
        ``mcp`` (maps ``is_error`` -> ERROR), the LLM recogniser otherwise
        (parses a ``{"cosmo": ...}`` intent block out of the model's text).
        Pass ``recognize=False`` to treat the Neuron's raw output as a plain
        AGENT_OUTPUT.

        ``teach_intents`` controls whether ``COSMO_INTENT_SYSTEM_PROMPT`` is
        appended to the source's ``system`` prompt so the model actually
        knows the ``{"cosmo": ...}`` convention the recogniser parses.
        Default (``None``): True exactly when ``recognize`` is on and the
        source accepts a ``system=`` kwarg -- ``ollama``, ``openai`` and
        ``anthropic``. The OpenAI-compatible sources (``huggingface``,
        ``groq``, ``openrouter``, ``together``, ``mistral``) take their
        system prompt as a ``messages`` entry instead, and ``mcp`` is
        never taught. Pass False to opt out.
        """
        from cosmonapse.neuron import Neuron  # lazy: avoids import cycle

        if teach_intents is None:
            teach_intents = (
                recognize and source.lower() in _SYSTEM_CAPABLE_SOURCES
            )
        if teach_intents:
            if source.lower() not in _SYSTEM_CAPABLE_SOURCES:
                raise ValueError(
                    f"teach_intents=True is not supported for source "
                    f"{source!r}: its Neuron wrapper accepts no system= "
                    f"kwarg. Embed the convention in the prompt yourself "
                    f"(cosmonapse.axon.COSMO_INTENT_SYSTEM_PROMPT)."
                )
            existing = source_kwargs.get("system")
            source_kwargs["system"] = (
                f"{existing}\n\n{COSMO_INTENT_SYSTEM_PROMPT}"
                if existing else COSMO_INTENT_SYSTEM_PROMPT
            )

        # Neuron(...) returns a callable _BaseNeuron at runtime, but its
        # __new__ return type leaves mypy inferring the nominal `Neuron`;
        # cast to the NeuronFn the Axon expects.
        neuron_fn = cast(NeuronFn, Neuron(source=source, **source_kwargs))
        parser: OutputParser | None = None
        if recognize:
            parser = (
                _parse_mcp_intents
                if source.lower() == "mcp"
                else _parse_llm_intents
            )
        return cls(
            neuron_id=neuron_id,
            neuron_fn=neuron_fn,
            capabilities=capabilities,
            version=version,
            neuron_kind=neuron_kind,
            context_fetcher=context_fetcher,
            engrams=engrams,
            effectors=effectors,
            tool_standard=tool_standard,
            parallel_tools=parallel_tools,
            output_parser=parser,
            glia=glia,
            strict_output=strict_output,
        )

    @classmethod
    def ollama(cls, neuron_id: str, **kw: Any) -> Axon:
        """Axon paired with a local Ollama daemon. kwargs: ``model`` (required),
        ``endpoint``, ``system``, ``temperature``, ``max_tokens``, ``timeout``."""
        return cls.from_source("ollama", neuron_id=neuron_id, **kw)

    @classmethod
    def huggingface(cls, neuron_id: str, **kw: Any) -> Axon:
        """Axon paired with a HuggingFace TGI / OpenAI-compatible endpoint.
        kwargs: ``endpoint`` (required), ``model``, ``use_chat_api``,
        ``temperature``, ``max_new_tokens``, ``api_key``, ``timeout``."""
        return cls.from_source("huggingface", neuron_id=neuron_id, **kw)

    # Alias matching the Neuron factory's ``"hf"``.
    hf = huggingface

    @classmethod
    def openai(cls, neuron_id: str, **kw: Any) -> Axon:
        """Axon paired with the OpenAI Chat Completions API. kwargs: ``model``
        (required), ``api_key`` (or ``OPENAI_API_KEY``), ``endpoint``,
        ``temperature``, ``max_tokens``, ``system``, ``timeout``."""
        return cls.from_source("openai", neuron_id=neuron_id, **kw)

    @classmethod
    def anthropic(cls, neuron_id: str, **kw: Any) -> Axon:
        """Axon paired with the Anthropic Messages API. kwargs: ``model``
        (required), ``api_key`` (or ``ANTHROPIC_API_KEY``), ``system``,
        ``max_tokens``, ``temperature``, ``timeout``."""
        return cls.from_source("anthropic", neuron_id=neuron_id, **kw)

    @classmethod
    def mcp(cls, neuron_id: str, **kw: Any) -> Axon:
        """Axon paired with a stdio MCP server. kwargs: ``command`` + ``args``
        or ``server`` (preset) + ``args``, plus ``env``, ``cwd``, ``tool``."""
        return cls.from_source("mcp", neuron_id=neuron_id, **kw)

    # -- recognition decorators ---------------------------------------
    # The decorator model. These are the asking side: ``detects_*`` registers
    # a *detector* over the Neuron's raw output, deliberately named apart from
    # the Dendrite's ``on_*`` handlers (which consume inbound Signals off the
    # bus). Each detector returns the intent's fields (a dict) to match, or
    # None to fall through to the next detector / capability. Detectors may be
    # sync or async; multiple per capability are tried in registration order.
    # Applied in precedence error -> clarification -> permission -> output by
    # ``_apply_recognisers``; they compose with (and run after) any
    # ``output_parser`` and before the literal ``__marker__`` checks.

    def before_task(self, fn: Callable[[dict[str, Any]], Any]) -> Callable[[dict[str, Any]], Any]:
        """Register a pre-task hook over the TASK's ``input`` dict.

        Runs before ``neuron_fn`` (and before the engram helpers are
        invoked by it). Sync or async; multiple hooks run in registration
        order, each receiving the previous one's result. A hook may:

        * return a (new) dict  -  replaces the input passed onward;
        * return ``None``      -  input passes through unchanged;
        * raise               -  the TASK is rejected; the exception
          surfaces as an ERROR Signal (code ``NEURON_EXCEPTION``).

        The natural place for input normalisation - e.g. reshaping a
        re-dispatched clarification follow-up. NOT the place for policy
        checks: those belong on a Glia card, whose gate reads the inbound
        TASK BEFORE these hooks so a policy sees what actually arrived
        rather than what a prompt builder reshaped. See ``cosmonapse.glia``
        and design/GLIA_DESIGN.md section 17.
        """
        self._before_task_hooks.append(fn)
        return fn

    async def _apply_before_task(self, input_data: dict[str, Any]) -> dict[str, Any]:
        for fn in self._before_task_hooks:
            r = fn(input_data)
            if inspect.isawaitable(r):
                r = await r
            if r is not None:
                input_data = r
        return input_data

    def detects_output(self, fn: Callable[[Any], Any]) -> Callable[[Any], Any]:
        """Detector returning the AGENT_OUTPUT payload dict, or None to leave
        the raw output to be wrapped verbatim."""
        self._recognisers["output"].append(fn)
        return fn

    def detects_clarification(self, fn: Callable[[Any], Any]) -> Callable[[Any], Any]:
        """Detector returning ``{"question": ..., "context": ...}`` to emit
        CLARIFICATION, or None."""
        self._recognisers["clarification"].append(fn)
        return fn

    def detects_permission(self, fn: Callable[[Any], Any]) -> Callable[[Any], Any]:
        """Detector returning ``{"action": ..., "scope": ..., "reason": ...,
        "context": ...}`` to emit PERMISSION, or None."""
        self._recognisers["permission"].append(fn)
        return fn

    def detects_error(self, fn: Callable[[Any], Any]) -> Callable[[Any], Any]:
        """Detector returning ``{"code": ..., "message": ..., "recoverable":
        ...}`` to emit ERROR, or None."""
        self._recognisers["error"].append(fn)
        return fn

    async def _apply_recognisers(self, raw: Any) -> tuple[Any, bool]:
        """Run registered detectors in precedence.

        Returns the marker dict from the first match and True, or the
        unchanged ``raw`` and False. The flag is what makes an UNCLAIMED
        output distinguishable from a plain answer that simply needed no
        recognising - the prerequisite for the repair loop, because
        otherwise both look identical here (GLIA_DESIGN section 8.2).
        """
        rec = self._recognisers
        if not any(rec.values()):
            return raw, False

        async def _first(fns: list[Callable[[Any], Any]]) -> Any:
            for fn in fns:
                r = fn(raw)
                if inspect.isawaitable(r):
                    r = await r
                if r is not None:
                    return r
            return None

        hit = await _first(rec["error"])
        if hit is not None:
            return {"__error__": True, **hit}, True
        hit = await _first(rec["clarification"])
        if hit is not None:
            return {"__clarification__": True, **hit}, True
        hit = await _first(rec["permission"])
        if hit is not None:
            return {"__permission__": True, **hit}, True
        hit = await _first(rec["output"])
        if hit is not None:
            return hit, True
        return raw, False

    # -- attachment ----------------------------------------------------

    @property
    def dendrite(self) -> Dendrite | None:
        return self._dendrite

    @property
    def host(self) -> _HostProxy:
        """Deferred Dendrite decorators - see :class:`_HostProxy`."""
        return _HostProxy(self)

    def attach_to(self, dendrite: Dendrite) -> None:
        if self._dendrite is not None and self._dendrite is not dendrite:
            raise RuntimeError(
                f"Axon {self.neuron_id!r} is already attached to a different Dendrite"
            )
        self._dendrite = dendrite

    def detach(self) -> None:
        self._dendrite = None

    # -- driven by the Dendrite ---------------------------------------

    async def _on_register_emitted(self) -> None:
        """Called by the Dendrite right after it emits REGISTER for us.
        Replays ``@host.on_*`` registrations onto the hosting Dendrite,
        fires on_connect hooks once, starts on_schedule loops."""
        if self._host_regs and not self._host_regs_applied:
            self._host_regs_applied = True
            assert self._dendrite is not None
            for name, _st, filters, fn in self._host_regs:
                getattr(self._dendrite, name)(fn, **filters)
            await self._dendrite.ensure_subscribed(
                *{st for _, st, _, _ in self._host_regs})
        self._launch_schedule()
        await self._fire_connect()

    async def _on_heartbeat_tick(self) -> None:
        """Called by the Dendrite on every heartbeat. Fires on_refresh."""
        await self._fire_refresh(RefreshEvent(
            reason="heartbeat",
            neuron_id=self.neuron_id,
        ))

    async def _on_deregister_emitted(self) -> None:
        """Called by the Dendrite during stop(). Tears down schedule loops and
        releases any resources the Neuron holds (e.g. a spawned MCP server)."""
        await self._stop_hooks()
        aclose = getattr(self._fn, "aclose", None)
        if callable(aclose):
            try:
                await aclose()
            except Exception:
                logger.warning("Axon %s: neuron aclose() failed", self.neuron_id, exc_info=True)

    # -- core: handle one TASK ----------------------------------------

    async def handle_task(self, task: Signal) -> Signal:
        """Run the Neuron and return AGENT_OUTPUT / CLARIFICATION / ERROR.

        Binds the TASK's (trace_id, parent_id=task.id) as the ambient trace
        context for the whole handling pass - neuron_fn, detectors, and
        lifecycle hooks included - so engram calls made without explicit
        trace plumbing (e.g. ``dendrite.imprint`` from a
        ``@detects_output`` hook) are attributed to this task's trace.

        With a Glia card mounted, also binds this Axon's caller gate for
        the same pass, so every ``recall`` / ``imprint`` / ``call_tool``
        the Neuron makes, however it reaches the Dendrite, is read
        outbound and its reply inbound.
        """
        gate = self._caller_gate(task)
        with trace_context(task.trace_id, task.id), caller_gate(gate):
            return await self._handle_task_inner(task, gate)

    def _caller_gate(self, task: Signal) -> CallerGate | None:
        card = self._glia
        if card is None or card.mode is Mode.OFF or not card.has_policies:
            return None
        return CallerGate(
            card,
            component=self.neuron_id,
            dendrite=self._dendrite,
            trace_id=task.trace_id,
            parent_id=task.id,
            can_reask=self.repair is not None,
        )

    async def _handle_task_inner(
        self, task: Signal, gate: CallerGate | None = None,
    ) -> Signal:
        trace_id = task.trace_id
        parent_id = task.id
        input_data: dict[str, Any] = task.payload.get("input", {})
        context_ref: str | None = task.payload.get("context_ref")

        # Build helpers bound to this TASK's trace/parent context. The
        # helpers are no-ops (raise EngramNotBound) when no bindings are
        # declared, so a misconfigured Neuron fails loudly.
        kwargs: dict[str, Any] = {}
        if self._fn_accepts_recall:
            kwargs["recall"] = self._build_recall_helper(trace_id, parent_id)
        if self._fn_accepts_imprint:
            kwargs["imprint"] = self._build_imprint_helper(trace_id, parent_id)
        if self._fn_accepts_call_tool:
            kwargs["call_tool"] = self._build_call_tool_helper(
                trace_id, parent_id,
            )

        # The gate, inbound: the TASK as it arrived. Deliberately BEFORE
        # the before_task hooks, so policy sees what actually arrived, not
        # what the developer's prompt builder reshaped. The whole gate
        # sits behind a null check on the card, the same shape as the
        # ``if self._before_task_hooks`` below, so an Axon with no card
        # executes no extra branches.
        card = self._glia
        # meta.glia accumulated over this pass and attached to whichever
        # Signal the TASK finally produces.
        pol: dict[str, Any] = {}
        if card is not None:
            oc = await card.check(
                task, direction=Direction.INBOUND, component=self.neuron_id,
            )
            if oc is not None:
                if oc.record:
                    pol.update(oc.record)
                # One AUDIT per gate event, at the event. This is what
                # makes the absence of a AUDIT on a trace mean "nothing
                # fired" instead of "nothing was recorded". There is no
                # retry for an inbound TASK: the input arrived from
                # outside and no attempt of ours changes what arrived.
                await self._emit_guard_audit(
                    oc, card, trace_id=trace_id, parent_id=parent_id,
                )
                if oc.denied:
                    return self._policy_refusal(
                        oc, trace_id=trace_id, parent_id=parent_id, pol=pol,
                    )
                if oc.escalated:
                    return self._policy_escalation(
                        oc, trace_id=trace_id, parent_id=parent_id, pol=pol,
                        action=f"run {self.neuron_id!r} on this input",
                    )
                if oc.redacted and isinstance(oc.replacement, dict):
                    # A redaction is the new payload, applied to a COPY,
                    # never in place: MemorySynapse hands the same Signal
                    # object to every in-process subscriber (hard
                    # constraint 7).
                    input_data = oc.replacement.get("input") or {}
                    context_ref = oc.replacement.get("context_ref")

        context: list[Any] = []
        if context_ref:
            try:
                context = await self._context_fetcher(context_ref)
            except Exception as exc:
                logger.warning(
                    "Axon %s: context fetch failed for %r: %s",
                    self.neuron_id, context_ref, exc,
                )

        # --- the repair loop ----------------------------------------
        # One loop, three callers: an unclaimed output, a tool call whose
        # arguments failed validation, and a policy refusal. All three
        # are correctable by re-asking the generator, which is why the
        # loop lives HERE and nowhere else - only the Axon holds a
        # generator it can re-ask, so the input CHANGES between attempts.
        #
        # Deliberately not the Effector/Engram transient retry: there the
        # input is identical and the hope is that the world changed. A
        # policy refusal never feeds that one, because retrying an
        # identical request against a deterministic policy is N
        # guaranteed denials (GLIA_DESIGN section 8.1).
        repair = self.repair

        # A trace already past the limit is the LOOP being broken rather
        # than the request, and that is the one legitimate ERROR here.
        if repair is not None and self._repair_count(trace_id) > repair.max_attempts:
            return self._repair_exhausted_error(
                trace_id=trace_id, parent_id=parent_id, pol=pol,
                limit=repair.max_attempts,
            )

        attempts: list[dict[str, Any]] = []
        pass_input = input_data
        attempt = 0
        loop_started = time.monotonic()
        while True:
            started = time.monotonic()
            result = await self._one_pass(
                pass_input, context, kwargs,
                trace_id=trace_id, parent_id=parent_id, pol=pol, card=card,
            )
            if isinstance(result, Signal):
                # The gate, outbound: whatever this pass is about to reply.
                result = await self._gate_reply(
                    result, trace_id=trace_id, parent_id=parent_id, pol=pol,
                    allow_reask=repair is not None,
                )
            took_ms = int((time.monotonic() - started) * 1000)
            if gate is not None:
                # Refusals the Neuron caught and swallowed still happened.
                await gate.flush(
                    consumed=result.refusal
                    if isinstance(result, _Repair) else None,
                )
            if isinstance(result, Signal):
                return self._with_attempts(result, attempts, pol)

            record = result.as_record(attempt + 1, took_ms)
            attempts.append(record)
            self._record_repair(trace_id, record)

            out_of_attempts = (
                repair is None
                or attempt >= repair.max_attempts
                or (
                    result.max_attempts is not None
                    and attempt >= result.max_attempts
                )
            )
            over_budget = (
                repair is not None
                and repair.deadline_s is not None
                and (time.monotonic() - loop_started) >= repair.deadline_s
            )
            if out_of_attempts or over_budget:
                if over_budget and not out_of_attempts:
                    # The time budget ran out before the attempts did.
                    # Recorded as its own category: nothing was retried
                    # here, the loop gave up, and an auditor must not
                    # read that as a generator that would not comply.
                    kind = AuditKind.DEADLINE_ABANDONED
                    record["kind"] = kind.value
                    record["domain"] = AUDIT_DOMAIN[kind]
                else:
                    kind = result.kind
                record["outcome"] = AuditOutcome.EXHAUSTED.value
                await self._emit_audit(
                    kind, AuditOutcome.EXHAUSTED, result,
                    attempt=attempt + 1, took_ms=took_ms,
                    trace_id=trace_id, parent_id=parent_id,
                )
                return self._with_attempts(
                    await self._final_fallback(
                        result, trace_id=trace_id, parent_id=parent_id,
                        pol=pol,
                    ),
                    attempts, pol,
                )

            next_input = await self._build_repair_input(
                pass_input, result, attempt + 1,
            )
            if canonical_text(next_input) == canonical_text(pass_input):
                # Nothing changed, so the next attempt cannot differ.
                # Stopping is the same rule that keeps a policy refusal
                # out of transient retry.
                record["outcome"] = AuditOutcome.UNCORRECTABLE.value
                await self._emit_audit(
                    result.kind, AuditOutcome.UNCORRECTABLE, result,
                    attempt=attempt + 1, took_ms=took_ms,
                    trace_id=trace_id, parent_id=parent_id,
                )
                logger.info(
                    "Axon %s: repair produced an identical input; not "
                    "re-asking. Register @axon.repairs, or fold "
                    "input['repair'] into the prompt in @axon.before_task, "
                    "so an attempt can actually differ.",
                    self.neuron_id,
                )
                return self._with_attempts(
                    await self._final_fallback(
                        result, trace_id=trace_id, parent_id=parent_id,
                        pol=pol,
                    ),
                    attempts, pol,
                )
            # Re-asking. The record goes out now, not when the next
            # attempt finishes, so a hang shows up as a re_asked record
            # with nothing after it.
            await self._emit_audit(
                result.kind, AuditOutcome.RE_ASKED, result,
                attempt=attempt + 1, took_ms=took_ms,
                trace_id=trace_id, parent_id=parent_id,
            )
            pass_input = next_input
            attempt += 1

    async def _one_pass(
        self,
        input_data: dict[str, Any],
        context: list[Any],
        kwargs: dict[str, Any],
        *,
        trace_id: str,
        parent_id: str,
        pol: dict[str, Any],
        card: Glia | None,
    ) -> Signal | _Repair:
        """One attempt at a TASK: run the Neuron, recognise, act.

        Returns the reply, or a :class:`_Repair` carrying the reason and
        the reply to send if the loop cannot fix it. The inbound gate on
        the TASK already ran in the caller, exactly once: the input
        arrived from outside and no attempt of ours changes what arrived.
        The outbound gate on the reply runs in the caller too, after this
        returns, so every reply this pass can produce passes it.
        """
        # Correctable tool-call outcomes, filled by _run_tool_call.
        repairs: list[_Repair] = []
        try:
            if self._before_task_hooks:
                input_data = await self._apply_before_task(input_data)
            # Native tool channel: hand the provider its own tools=
            # payload. Injected AFTER the before_task hooks so a hook
            # that rebuilds the input from scratch (the usual prompt
            # builder) cannot drop it. Only ever present when a binding
            # declared schemas.
            if self._native_tools is not None and isinstance(input_data, dict):
                input_data = {**input_data, "tools": self._native_tools}
            if kwargs:
                raw_output: dict[str, Any] = await self._fn(
                    input_data, context, **kwargs,
                )
            else:
                raw_output = await self._fn(input_data, context)
            # Tool-call recognition. Structured calls off the provider's
            # own channel first, then the declared/inferred text dialect.
            # Either way a match takes precedence over the cosmo parser
            # and the recognisers - a tool call IS the intent, there is
            # nothing further to recognise.
            native_calls: list[dict[str, Any]] = []
            if self._tools_enabled:
                native_calls = extract_tool_calls(
                    raw_output, self._tool_standard,
                )
            if not native_calls:
                # Per-source recognition: turn the Neuron's native output
                # (LLM text, MCP result) into the marker dict the branches
                # below understand. Runs inside the try so a parser failure
                # surfaces as an ERROR Signal rather than crashing the
                # Dendrite.
                if self._output_parser is not None:
                    raw_output = self._output_parser(raw_output)
                # Decorator-registered recognisers
                # (@axon.detects_clarification, ...) run after the parser
                # and may convert output into a marker.
                raw_output, claimed = await self._apply_recognisers(
                    raw_output,
                )
                if not claimed and self._strict_output and self._judgeable:
                    _raise_unclaimed(raw_output)
        except PolicyRefusal as exc:
            # The gate refused one of the Neuron's own calls (or a reply to
            # one), and the Neuron let the refusal propagate. It is handed
            # back here to do what the policy asked.
            return self._on_call_refusal(
                exc, trace_id=trace_id, parent_id=parent_id, pol=pol,
            )
        except InvalidOutput as exc:
            # Correctable, not terminal. The fallback is exactly what
            # this Axon returned before strict mode existed.
            return _Repair(
                kind=AuditKind.EVAL_RETRY,
                problem=str(exc),
                fallback=agent_output_signal(
                    trace_id=trace_id,
                    parent_id=parent_id,
                    directed=Directed(id=self.neuron_id),
                    output=(
                        exc.output if isinstance(exc.output, dict)
                        else {"value": exc.output}
                    ),
                    meta=self._policy_meta(pol),
                ),
            )
        except Exception as exc:
            logger.exception("Axon %s: Neuron raised", self.neuron_id)
            return error_signal(
                trace_id=trace_id,
                parent_id=parent_id,
                directed=Directed(id=self.neuron_id),
                code="NEURON_EXCEPTION",
                message=str(exc),
                recoverable=False,
                meta=self._policy_meta(pol),
            )

        # Native tool call: translate-and-act. With a serving binding the
        # Axon dispatches through the EffectorClient and the AGENT_OUTPUT
        # carries the observation; with no bindings at all it carries the
        # translated call for the host chain to execute (pure translation).
        if native_calls:
            reply = await self._dispatch_native_tool_calls(
                native_calls, trace_id=trace_id, parent_id=parent_id,
                pol=pol, repairs=repairs,
            )
            if repairs:
                # A refused or invalid call is the model's to correct, and
                # the observation it would have received is the fallback.
                first = repairs[0]
                first.fallback = reply
                return first
            return reply

        # Error marker: a recogniser (e.g. MCP ``is_error``) can request an
        # ERROR Signal without raising. Same return-surface as a raised
        # exception, but with a recogniser-supplied code/message.
        if isinstance(raw_output, dict) and raw_output.get("__error__"):
            return error_signal(
                trace_id=trace_id,
                parent_id=parent_id,
                directed=Directed(id=self.neuron_id),
                code=raw_output.get("code", "NEURON_ERROR"),
                message=raw_output.get("message", ""),
                recoverable=bool(raw_output.get("recoverable", False)),
                meta=self._policy_meta(pol),
            )

        if isinstance(raw_output, dict) and raw_output.get("__clarification__"):
            return clarification_signal(
                trace_id=trace_id,
                parent_id=parent_id,
                directed=Directed(id=self.neuron_id),
                question=raw_output.get("question", ""),
                context=raw_output.get("context"),
                meta=self._policy_meta(pol),
            )

        # Permission marker: same return-and-resume shape as clarification.
        # A Neuron typically tries `recall(...)` first and only returns this
        # marker on a miss; the orchestrator decides, imprints the grant, and
        # re-dispatches via respond_to_permission so the Neuron resumes (and
        # can imprint/recall the decision itself).
        if isinstance(raw_output, dict) and raw_output.get("__permission__"):
            return permission_signal(
                trace_id=trace_id,
                parent_id=parent_id,
                directed=Directed(id=self.neuron_id),
                action=raw_output.get("action", ""),
                scope=raw_output.get("scope"),
                reason=raw_output.get("reason"),
                context=raw_output.get("context"),
                meta=self._policy_meta(pol),
            )

        return agent_output_signal(
            trace_id=trace_id,
            parent_id=parent_id,
            directed=Directed(id=self.neuron_id),
            output=raw_output if isinstance(raw_output, dict) else {"value": raw_output},
            meta=self._policy_meta(pol),
        )

    # ------------------------------------------------------------------
    # Policy plumbing (Glia)
    # ------------------------------------------------------------------

    @staticmethod
    def _policy_meta(pol: dict[str, Any]) -> dict[str, Any] | None:
        """``meta`` carrying the thin policy record, or None when there is
        nothing to say. Thin by design: verdict, policy id, policy
        version, direction, signal type, component and a hash. The matched content
        stays in the component's local journal, or the audit record leaks
        exactly what the policy was protecting."""
        return {META_KEY: dict(pol)} if pol else None

    def _policy_refusal(
        self,
        oc: PolicyOutcome,
        *,
        trace_id: str,
        parent_id: str,
        pol: dict[str, Any],
    ) -> Signal:
        """The reply a refused step would have produced anyway, with
        ``error`` set and ``meta.glia`` attached.

        Never an ERROR. An ERROR closes the Pathway unconditionally and,
        when flagged recoverable, is re-dispatched by ``default_retry_on``
        onto a fresh trace - which STOPs the abandoned attempt and can
        saga-roll-back its Engram writes. A policy deny as a recoverable
        ERROR means three guaranteed denials, three STOPs and three
        rollbacks (GLIA_DESIGN section 12.1).
        """
        return mark_exempt(agent_output_signal(
            trace_id=trace_id,
            parent_id=parent_id,
            directed=Directed(id=self.neuron_id),
            output={
                "error": oc.reason or "refused by policy",
                "refused_by": "policy",
                "scope": oc.scope,
            },
            meta=self._policy_meta(pol),
        ))

    def _policy_escalation(
        self,
        oc: PolicyOutcome,
        *,
        trace_id: str,
        parent_id: str,
        pol: dict[str, Any],
        action: str,
        context: dict[str, Any] | None = None,
    ) -> Signal:
        """PERMISSION instead of running.

        ``escalate`` needs no new machinery: ``permission_signal`` and
        ``respond_to_permission`` already carry a verdict back as a new
        TASK parented to the PERMISSION, on the original trace. Until now
        only a cooperative Neuron could open that channel by returning a
        ``__permission__`` marker; a card lets the runtime open it.
        """
        ctx: dict[str, Any] = {"scope": oc.scope}
        if oc.declared.policy_id:
            ctx["policy_id"] = oc.declared.policy_id
        if context:
            ctx.update(context)
        return mark_exempt(permission_signal(
            trace_id=trace_id,
            parent_id=parent_id,
            directed=Directed(id=self.neuron_id),
            action=action,
            reason=oc.reason,
            context=ctx,
            meta=self._policy_meta(pol),
        ))


    # ------------------------------------------------------------------
    # Engram helper plumbing (called from handle_task)
    # ------------------------------------------------------------------

    def _engram_client(self) -> Any:
        if self._dendrite is None:
            raise RuntimeError(
                f"Axon {self.neuron_id!r}: not attached to a Dendrite; "
                f"engram helpers require a hosting Dendrite"
            )
        return self._dendrite.engram_client

    def _resolve_binding(self, name: str) -> EngramBinding:
        binding = self._engram_bindings.get(name)
        if binding is None:
            raise EngramNotBound(
                f"Axon {self.neuron_id!r}: no Engram binding named {name!r}; "
                f"available: {sorted(self._engram_bindings)}"
            )
        return binding

    def _build_recall_helper(self, trace_id: str, parent_id: str) -> Any:
        async def _recall(
            name: str,
            *,
            query: dict[str, Any],
            filters: dict[str, Any] | None = None,
            context_ref: str | None = None,
            deadline_ms: int | None = None,
            recall_mode: str | None = None,
            min_confidence: float | None = None,
            meta: dict[str, Any] | None = None,
        ) -> Any:
            binding = self._resolve_binding(name)
            client = self._engram_client()
            return await client.recall(
                binding=binding,
                query=query,
                filters=filters,
                context_ref=context_ref,
                deadline_ms=deadline_ms,
                recall_mode=recall_mode,
                min_confidence=min_confidence,
                trace_id=trace_id,
                parent_id=parent_id,
                neuron=self.neuron_id,
                meta=meta,
            )
        return _recall

    def _build_imprint_helper(self, trace_id: str, parent_id: str) -> Any:
        async def _imprint(
            name: str,
            *,
            op: str,
            entry: dict[str, Any],
            merge_key: str | None = None,
            await_ack: bool = False,
            deadline_ms: int | None = None,
            meta: dict[str, Any] | None = None,
        ) -> Any:
            binding = self._resolve_binding(name)
            client = self._engram_client()
            return await client.imprint(
                binding=binding,
                op=op,
                entry=entry,
                merge_key=merge_key,
                await_ack=await_ack,
                deadline_ms=deadline_ms,
                trace_id=trace_id,
                parent_id=parent_id,
                neuron=self.neuron_id,
                meta=meta,
            )
        return _imprint

    @property
    def engram_bindings(self) -> dict[str, EngramBinding]:
        return dict(self._engram_bindings)

    # ------------------------------------------------------------------
    # Effector helper plumbing (called from handle_task)
    # ------------------------------------------------------------------

    def _effector_client(self) -> Any:
        if self._dendrite is None:
            raise RuntimeError(
                f"Axon {self.neuron_id!r}: not attached to a Dendrite; "
                f"effector helpers require a hosting Dendrite"
            )
        return self._dendrite.effector_client

    def _resolve_effector_binding(self, name: str) -> EffectorBinding:
        binding = self._effector_bindings.get(name)
        if binding is None:
            raise EffectorNotBound(
                f"Axon {self.neuron_id!r}: no Effector binding named "
                f"{name!r}; available: {sorted(self._effector_bindings)}"
            )
        return binding

    def _resolve_binding_for_tool(self, tool: str) -> EffectorBinding | None:
        """Which binding serves ``tool``? (1) a binding whose ``tools``
        lists it, (2) a binding named after it, (3) the only binding when
        exactly one is declared. None on no match - never a guess between
        several."""
        for b in self._effector_bindings.values():
            if b.tools and tool in b.tools:
                return b
        named = self._effector_bindings.get(tool)
        if named is not None:
            return named
        if len(self._effector_bindings) == 1:
            return next(iter(self._effector_bindings.values()))
        return None

    def _build_call_tool_helper(self, trace_id: str, parent_id: str) -> Any:
        async def _call_tool(
            name: str,
            *,
            tool: str,
            args: dict[str, Any] | None = None,
            call_id: str | None = None,
            deadline_ms: int | None = None,
            meta: dict[str, Any] | None = None,
        ) -> Any:
            binding = self._resolve_effector_binding(name)
            client = self._effector_client()
            return await client.call(
                binding=binding,
                tool=tool,
                args=args,
                call_id=call_id,
                deadline_ms=deadline_ms,
                trace_id=trace_id,
                parent_id=parent_id,
                neuron=self.neuron_id,
                meta=meta,
            )
        return _call_tool

    async def _dispatch_native_tool_calls(
        self,
        calls: list[dict[str, Any]],
        *,
        trace_id: str,
        parent_id: str,
        pol: dict[str, Any] | None = None,
        repairs: list[_Repair] | None = None,
    ) -> Signal:
        """Act on the recognised tool calls and wrap the observation(s).

        The FIRST call's observation is always the top-level payload, so
        every existing host chain reads exactly what it read before. What
        is new is that the others are accounted for: with
        ``parallel_tools=True`` they run too and every observation is
        listed under ``calls``; with it off they are named under
        ``dropped_calls``. A model that asked for three actions and got
        one must not be told it got three.

        Always returns AGENT_OUTPUT: a tool failure (timeout, tool-level
        error, no serving binding, invalid arguments) rides ``error`` in
        the output payload for the Neuron/host to react to - it never
        terminates the TASK.
        """
        pol = pol if pol is not None else {}
        try:
            if self._parallel_tools:
                # Sequential on purpose despite the name: the provider
                # emits these as a batch, but they are still ORDERED, and
                # tools have side effects (a write the next call reads
                # back). Running them in reply order is the only
                # interleaving the model can reason about.
                observations = [
                    await self._run_tool_call(
                        c, trace_id=trace_id, parent_id=parent_id, pol=pol,
                        repairs=repairs,
                    )
                    for c in calls
                ]
                out = dict(observations[0])
                if len(observations) > 1:
                    out["calls"] = observations
            else:
                out = await self._run_tool_call(
                    calls[0], trace_id=trace_id, parent_id=parent_id, pol=pol,
                    repairs=repairs,
                )
        except _ToolCallEscalation as esc:
            return self._policy_escalation(
                esc.outcome, trace_id=trace_id, parent_id=parent_id, pol=pol,
                action=f"call tool {esc.tool!r} from {self.neuron_id!r}",
            )
        if not self._parallel_tools and len(calls) > 1:
            out["dropped_calls"] = [
                {"tool": c["tool"], "args": c.get("args") or {},
                 "call_id": c.get("call_id")}
                for c in calls[1:]
            ]
            logger.info(
                "Axon %s: %d tool call(s) not run (parallel_tools=False): %s",
                self.neuron_id, len(calls) - 1,
                [c["tool"] for c in calls[1:]],
            )
        return agent_output_signal(
            trace_id=trace_id,
            parent_id=parent_id,
            directed=Directed(id=self.neuron_id),
            output=out,
            meta=self._policy_meta(pol),
        )

    async def _run_tool_call(
        self,
        call: dict[str, Any],
        *,
        trace_id: str,
        parent_id: str,
        pol: dict[str, Any] | None = None,
        repairs: list[_Repair] | None = None,
    ) -> dict[str, Any]:
        """Resolve, validate and dispatch ONE call; return its observation.

        With no bindings declared the translated call passes through
        unexecuted (``{"tool", "args", "call_id"}``) for the host chain
        to run - the pre-binding harness pattern, minus the hand-written
        parser.
        """
        tool = call["tool"]
        args = call.get("args") or {}
        call_id = call.get("call_id")
        out: dict[str, Any] = {"tool": tool, "args": args}
        if call_id is not None:
            out["call_id"] = call_id

        if not self._effector_bindings:
            return out

        binding = self._resolve_binding_for_tool(tool)
        if binding is None:
            out["error"] = (
                f"no effector binding serves tool {tool!r}; "
                f"bound: {sorted(self._effector_bindings)}"
            )
            return out

        # Argument validation against the declared schema, before the
        # call leaves the process. The message goes back as the tool
        # observation, which is the one channel the model reliably reads
        # and can correct from - far better than a stack trace from the
        # far side of the wire, or a tool that half-runs on bad input.
        schema = binding.schema_for(tool)
        if schema is not None:
            problem = validate_args(schema, args)
            if problem is not None:
                out["error"] = problem
                logger.info(
                    "Axon %s: rejected call to %r: %s",
                    self.neuron_id, tool, problem,
                )
                if repairs is not None:
                    repairs.append(_Repair(
                        kind=AuditKind.TOOL_RETRY,
                        problem=problem,
                    ))
                return out

        try:
            # The gate, both ways, runs inside the client: the TOOL_CALL
            # outbound before it is published, the TOOL_RESULT inbound
            # before this reads it.
            outcome = await self._effector_client().call(
                binding=binding,
                tool=tool,
                args=args,
                call_id=call_id,
                deadline_ms=(
                    binding.default_deadline_ms
                    if binding.default_deadline_ms is not None
                    else DEFAULT_TOOL_DEADLINE_MS
                ),
                trace_id=trace_id,
                parent_id=parent_id,
                neuron=self.neuron_id,
            )
        except PolicyRefusal as exc:
            if pol is not None and exc.record:
                pol.update(exc.record)
            if exc.decision is Decision.ESCALATE:
                raise _ToolCallEscalation(exc.outcome, tool) from None
            # The existing validate_args shape, so every host chain reads
            # a refusal exactly as it reads a rejected argument, and the
            # model reads it where it reads every tool observation.
            out["error"] = exc.reason
            out["refused_by"] = "policy"
            logger.info(
                "Axon %s: policy refused %s for tool %r",
                self.neuron_id, exc.outcome.scope, tool,
            )
            if exc.retry is Retry.REASK and repairs is not None:
                repairs.append(_Repair.from_refusal(exc))
        except EffectorError as exc:
            out["error"] = f"{type(exc).__name__}: {exc}"
        except Exception as exc:
            logger.exception(
                "Axon %s: tool dispatch for %r raised", self.neuron_id, tool,
            )
            out["error"] = f"tool_dispatch_failed: {exc}"
        else:
            out["effector_id"] = outcome.effector_id
            if outcome.error is not None:
                out["error"] = outcome.error
            else:
                out["result"] = outcome.result
        return out

    @property
    def glia(self) -> Glia | None:
        """The mounted policy card, if any. None is the default and means
        this Axon's gate reads nothing."""
        return self._glia

    # ------------------------------------------------------------------
    # Correction repair
    # ------------------------------------------------------------------

    @property
    def repair(self) -> RepairPolicy | None:
        """The effective correction-repair policy, or None for no loop.

        An explicit ``repair=`` wins; otherwise a mounted card's. With
        neither, the loop does not exist and this Axon behaves exactly as
        it did before it was written.
        """
        if self._repair is not None:
            return self._repair
        if self._glia is not None and self._glia.repair.max_attempts > 0:
            return self._glia.repair
        return None

    @property
    def _judgeable(self) -> bool:
        """Whether anything could have claimed an output.

        With no recognisers, no parser and no tool dialect there is
        nothing that *should* have matched, so ``strict_output`` has no
        opinion and stays silent rather than rejecting every plain
        answer.
        """
        return bool(
            self._output_parser is not None
            or self._tools_enabled
            or any(self._recognisers.values())
        )

    def repairs(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        """Register the builder for the next attempt's input.

        ``fn(input, problem)`` returns the input to re-ask with, plus
        ``attempt`` / ``kind`` / ``axon`` if declared as keyword
        parameters (``kind`` is the audit category, e.g. ``GUARD_RETRY``).
        Sync or async; handlers run in registration order and the first
        non-None return wins.

        WITHOUT one of these, the default builder merges a ``repair`` key
        into the input, which reaches the Neuron only if something
        downstream reads it - a ``@axon.before_task`` prompt builder, or
        a Neuron that inspects its own input. If nothing does, the input
        is unchanged in every way the provider can see, the loop detects
        that and stops on the first attempt rather than spending model
        calls on a request that cannot differ.
        """
        self._repair_builders.append(fn)
        return fn

    async def _build_repair_input(
        self, input_data: dict[str, Any], req: _Repair, attempt: int,
    ) -> dict[str, Any]:
        for fn in self._repair_builders:
            kwargs: dict[str, Any] = {}
            try:
                sig = inspect.signature(fn)
                names = set(sig.parameters)
                if any(
                    p.kind is inspect.Parameter.VAR_KEYWORD
                    for p in sig.parameters.values()
                ):
                    names |= {"attempt", "kind", "axon"}
            except (ValueError, TypeError):
                names = set()
            if "attempt" in names:
                kwargs["attempt"] = attempt
            if "kind" in names:
                kwargs["kind"] = req.kind.value
            if "axon" in names:
                kwargs["axon"] = self
            try:
                built = fn(input_data, req.problem, **kwargs)
                if inspect.isawaitable(built):
                    built = await built
            except Exception:
                logger.exception(
                    "Axon %s: @repairs builder raised; not re-asking",
                    self.neuron_id,
                )
                return input_data
            if isinstance(built, dict):
                return built
            if built is not None:
                logger.error(
                    "Axon %s: @repairs builder returned %s; a builder "
                    "returns a dict or None",
                    self.neuron_id, type(built).__name__,
                )
                return input_data
        return {
            **input_data,
            "repair": {
                "attempt": attempt,
                "kind": req.kind.value,
                "problem": req.problem,
            },
        }

    def _repair_count(self, trace_id: str) -> int:
        return len(self._repair_counts.get(trace_id, ()))

    def forget_trace(self, trace_id: str) -> None:
        """Drop the repair records for a finished trace. Called by the
        hosting Dendrite when it acks a STOP."""
        self._repair_counts.pop(trace_id, None)

    def repair_attempts(self, trace_id: str) -> list[dict[str, Any]]:
        """Repair attempts recorded on ``trace_id`` so far.

        Public because a STOP arriving mid-repair has to be able to put
        what happened onto the STOPPED ack: the repair runs before
        anything is published, so otherwise the cases most worth
        auditing emit nothing (GLIA_DESIGN section 9.2).
        """
        return list(self._repair_counts.get(trace_id, ()))

    def _record_repair(self, trace_id: str, record: dict[str, Any]) -> None:
        if not trace_id:
            return
        bucket = self._repair_counts.get(trace_id)
        if bucket is None:
            if len(self._repair_counts) >= _MAX_REPAIR_TRACES:
                self._repair_counts.pop(next(iter(self._repair_counts)), None)
            bucket = []
            self._repair_counts[trace_id] = bucket
        bucket.append(record)

    def _repair_fallback(
        self,
        req: _Repair,
        *,
        trace_id: str,
        parent_id: str,
        pol: dict[str, Any],
    ) -> Signal:
        if req.fallback is not None:
            return req.fallback
        return agent_output_signal(  # pragma: no cover - belt and braces
            trace_id=trace_id,
            parent_id=parent_id,
            directed=Directed(id=self.neuron_id),
            output={"error": req.problem},
            meta=self._policy_meta(pol),
        )

    def _repair_exhausted_error(
        self,
        *,
        trace_id: str,
        parent_id: str,
        pol: dict[str, Any],
        limit: int,
    ) -> Signal:
        """The one legitimate ERROR in this design.

        Past the limit the loop is broken, not the request, so this is
        not a refusal wearing an ERROR's clothes. It is flagged
        ``recoverable=False`` deliberately: ``default_retry_on`` retries
        an ERROR only when it is recoverable, so this one is not
        re-dispatched onto a fresh trace either (GLIA_DESIGN section
        8.4).
        """
        seen = self.repair_attempts(trace_id)
        repair = self.repair
        cap = repair.max_recorded if repair is not None else 8
        meta = {META_KEY: {
            **pol,
            F_ATTEMPT: len(seen),
            F_ATTEMPTS: seen[-cap:] if cap > 0 else [],
        }}
        return mark_exempt(error_signal(
            trace_id=trace_id,
            parent_id=parent_id,
            directed=Directed(id=self.neuron_id),
            code="REPAIR_EXHAUSTED",
            message=(
                f"repair limit of {limit} reached on this trace; the "
                f"correction loop is not converging"
            ),
            recoverable=False,
            meta=meta,
        ))

    def _with_attempts(
        self,
        reply: Signal,
        attempts: list[dict[str, Any]],
        pol: dict[str, Any],
    ) -> Signal:
        """Put the attempt summary on the reply.

        The repair runs before anything is published, so an observer
        cannot see it unless the Axon puts it on the wire. The DEFAULT is
        this summary: attempt number, trigger, outcome and duration per
        entry, capped - the policy id, never the model's rejected output.
        """
        if not attempts:
            return reply
        repair = self.repair
        cap = repair.max_recorded if repair is not None else 8
        record = dict(reply.meta.get(META_KEY) or pol)
        record[F_ATTEMPTS] = attempts[-cap:] if cap > 0 else []
        record[F_ATTEMPT] = len(attempts)
        reply.meta[META_KEY] = record
        return reply

    async def _emit_audit(
        self,
        kind: AuditKind,
        outcome: AuditOutcome,
        req: _Repair,
        *,
        attempt: int,
        took_ms: int,
        trace_id: str,
        parent_id: str,
    ) -> None:
        """One AUDIT record for one loop event.

        Emitted AT the event, not at the next attempt's start, so the
        number of records equals the number of events: a refusal that is
        never re-asked (``max_attempts=0``) still produces one, and so
        does the final refusal that exhausts the loop. That is the whole
        basis for reading the absence of any AUDIT on a trace as "nothing
        fired".

        A hang is still visible, because a ``re_asked`` record with
        nothing after it means the attempt it announced never finished.

        AUDIT is in SYNAPSE_TYPES and PATHWAY_TYPES but in none of the
        terminal, wait or scope-terminal sets, so it resolves no
        ``wait()``, closes no Pathway, and a terminal-scoped Pathway
        drops it. It also counts as nothing against a trace limit.
        """
        await publish_audit(
            self._dendrite,
            kind=kind,
            outcome=outcome,
            trace_id=trace_id,
            parent_id=parent_id,
            component=self.neuron_id,
            attempt=attempt,
            card=self._glia,
            direction=req.direction,
            signal_type=req.signal_type,
            policy_id=req.policy_id,
            reason=req.problem,
            digest=req.digest,
            took_ms=took_ms,
        )

    async def _emit_guard_audit(
        self,
        oc: PolicyOutcome,
        card: Glia | None,
        *,
        trace_id: str,
        parent_id: str,
    ) -> None:
        """One GUARDED record for a gate event the loop does not own.

        A refusal with no re-ask, a redaction and an escalation are not
        re-asked anywhere, so their records go out here rather than from
        the loop. A pass emits nothing at all, which is the point.

        In ``audit`` the outcome is ``would_block``: the verdict was
        recorded and nothing was blocked, and the record must not claim
        an action that did not happen.
        """
        if not oc.declared.shapes:
            return
        await audit_outcome(
            self._dendrite, oc,
            kind=AuditKind.GUARDED,
            outcome=outcome_for(oc.declared.decision.value, applied=oc.applied),
            trace_id=trace_id,
            parent_id=parent_id,
            component=self.neuron_id,
            card=card,
        )

    # ------------------------------------------------------------------
    # The gate, outbound, and refusals handed back by the Neuron
    # ------------------------------------------------------------------

    async def _gate_reply(
        self,
        reply: Signal,
        *,
        trace_id: str,
        parent_id: str,
        pol: dict[str, Any],
        allow_reask: bool,
    ) -> Signal | _Repair:
        """Read a reply leaving this Axon through the card's gate.

        Every reply a pass can produce comes through here: AGENT_OUTPUT,
        CLARIFICATION, PERMISSION and ERROR, and the AGENT_OUTPUT carrying
        a tool observation. A deny whose policy asks to ``reask`` feeds the
        repair loop, which writes the AUDIT record because only it knows
        whether the refusal was re-asked or final; anything else is
        recorded here and answered now.
        """
        card = self._glia
        if card is None or is_exempt(reply):
            return reply
        oc = await card.check(
            reply, direction=Direction.OUTBOUND, component=self.neuron_id,
        )
        if oc is None:
            return reply
        if oc.record:
            pol.update(oc.record)
        if not oc.shaped:
            await self._emit_guard_audit(
                oc, card, trace_id=trace_id, parent_id=parent_id,
            )
            return with_glia_record(reply, oc.record)
        if oc.redacted and isinstance(oc.replacement, dict):
            await self._emit_guard_audit(
                oc, card, trace_id=trace_id, parent_id=parent_id,
            )
            return with_glia_record(
                with_payload(reply, oc.replacement), oc.record,
            )
        if oc.escalated:
            await self._emit_guard_audit(
                oc, card, trace_id=trace_id, parent_id=parent_id,
            )
            # PERMISSION carrying the pending output, so the decider sees
            # what it is deciding about.
            return self._policy_escalation(
                oc, trace_id=trace_id, parent_id=parent_id, pol=pol,
                action=f"emit the output of {self.neuron_id!r}",
                context={"pending_output": _jsonable(reply.payload)},
            )
        refusal = self._policy_refusal(
            oc, trace_id=trace_id, parent_id=parent_id, pol=pol,
        )
        retry = allowed_retry(
            oc.retry, Direction.OUTBOUND, reply.type, can_reask=allow_reask,
        )
        if retry is Retry.REASK:
            return _Repair.from_refusal(PolicyRefusal(oc), fallback=refusal)
        await self._emit_guard_audit(
            oc, card, trace_id=trace_id, parent_id=parent_id,
        )
        return refusal

    def _on_call_refusal(
        self,
        exc: PolicyRefusal,
        *,
        trace_id: str,
        parent_id: str,
        pol: dict[str, Any],
    ) -> Signal | _Repair:
        """A refusal of one of the Neuron's own calls, handed back."""
        if exc.record:
            pol.update(exc.record)
        oc = exc.outcome
        if exc.decision is Decision.ESCALATE:
            return self._policy_escalation(
                oc, trace_id=trace_id, parent_id=parent_id, pol=pol,
                action=(
                    f"let {self.neuron_id!r} send "
                    f"{oc.signal_type.value}"
                ),
            )
        refusal = self._policy_refusal(
            oc, trace_id=trace_id, parent_id=parent_id, pol=pol,
        )
        if exc.retry is Retry.REASK:
            return _Repair.from_refusal(exc, fallback=refusal)
        return refusal

    async def _final_fallback(
        self,
        req: _Repair,
        *,
        trace_id: str,
        parent_id: str,
        pol: dict[str, Any],
    ) -> Signal:
        """The reply sent when the loop cannot fix it, through the gate.

        A policy refusal is gate-generated and passes unread. Anything
        else (an unclaimed output, a tool observation) is the Neuron's
        own and is read like any other reply, with no re-ask left.
        """
        reply = self._repair_fallback(
            req, trace_id=trace_id, parent_id=parent_id, pol=pol,
        )
        gated = await self._gate_reply(
            reply, trace_id=trace_id, parent_id=parent_id, pol=pol,
            allow_reask=False,
        )
        assert isinstance(gated, Signal)
        return gated

    @property
    def strict_output(self) -> bool:
        """Whether an unclaimed output raises ``InvalidOutput`` instead of
        passing through as the answer."""
        return self._strict_output

    @property
    def effector_bindings(self) -> dict[str, EffectorBinding]:
        return dict(self._effector_bindings)

    @property
    def tool_standard(self) -> str | None:
        return self._tool_standard

    @property
    def native_tools(self) -> list[dict[str, Any]] | None:
        """The provider-shaped ``tools=`` payload this Axon injects, or
        None when no binding declared schemas."""
        return self._native_tools


# ---------------------------------------------------------------------------
# Inferring the dialect from the wired Neuron
# ---------------------------------------------------------------------------
# Which tool-call dialect a model speaks, and which tools= shape its
# endpoint takes, are facts about the Neuron that was wired in - not
# separate decisions the caller should have to restate. Restating them
# is how they drift: swap the model, forget the string, and every tool
# call silently degrades into a final answer. An explicit tool_standard=
# still wins; this only fills the gap where construction used to fail.

#: Open models trained on the Nous/Hermes <tool_call> tag convention.
_HERMES_MODELS = (
    "qwen", "hermes", "nous", "functionary", "firefunction", "smollm",
)
#: Models trained on OpenAI-style function-calling JSON.
_CODEX_MODELS = (
    "gpt", "o1-", "o3", "o4", "llama", "mistral", "mixtral", "deepseek",
    "gemma", "phi-", "command-r", "granite",
)


def _provider_of(fn: Any) -> str:
    """Which provider's ``tools=`` shape this Neuron's endpoint takes."""
    name = type(fn).__name__.lower()
    if "anthropic" in name:
        return "anthropic"
    if "ollama" in name:
        return "ollama"
    # Everything else in the SDK speaks the OpenAI wire shape: OpenAI
    # itself, TGI/vLLM/llama.cpp, and the groq / together / openrouter /
    # mistral aliases that are _HuggingFaceNeuron pointed elsewhere.
    return "openai"


def _infer_tool_standard(fn: Any) -> str | None:
    """The text dialect ``fn`` most likely emits, or None if unknowable.

    None keeps the old contract intact: an Axon that declares effectors
    around a Neuron nothing can be inferred from still fails loudly at
    construction rather than parsing nothing at runtime. A recognised
    LLM wrapper whose model name is unfamiliar falls back to ``auto``,
    which tries every dialect - being unsure is not a reason to
    recognise nothing.
    """
    cls = type(fn).__name__.lower()
    if "mcp" in cls:
        return None          # an MCP server is a tool, not a tool CALLER
    if "anthropic" in cls:
        return "claude"
    model = getattr(fn, "model", None)
    if isinstance(model, str) and model:
        m = model.lower()
        if any(k in m for k in _HERMES_MODELS):
            return "hermes"
        if any(k in m for k in _CODEX_MODELS):
            return "codex"
    # A provider wrapper we recognise but a model name we do not.
    if cls.endswith("neuron"):
        return "auto"
    return None               # a plain callable: nothing to go on


# ---------------------------------------------------------------------------
# Per-source recognisers
# ---------------------------------------------------------------------------
# These map a Neuron's *native* output onto the marker dict Axon.handle_task
# already understands. They are the recognition half of the adapter and belong
# to the Axon, not the Neuron.
#
# Intent convention (LLM sources)
# -------------------------------
# A provider LLM returns free text. To request something other than a plain
# answer it emits a single JSON object carrying a ``cosmo`` key - as the whole
# response or inside a ```json fenced block:
#
#   {"cosmo": "clarification", "question": "which region?"}
#   {"cosmo": "permission", "action": "delete", "scope": "/db", "reason": "..."}
#   {"cosmo": "error", "code": "REFUSED", "message": "..."}
#   {"cosmo": "output", "output": {"answer": "..."}}
#
# Anything else (prose, or JSON without a ``cosmo`` key) is a normal output, so
# ordinary text never misfires.

# System-prompt fragment teaching an LLM the ``cosmo`` intent convention.
# Without it a hosted model never knows it *can* clarify / request
# permission / signal a structured error, so the recognisers below have
# nothing to recognise. ``Axon.from_source(recognize=True)`` appends this
# to the source's ``system`` prompt by default for system-capable LLM
# sources (opt out with ``teach_intents=False``).
COSMO_INTENT_SYSTEM_PROMPT = (
    "You can control the surrounding agent protocol by replying with a "
    "single JSON object carrying a \"cosmo\" key (either as your whole "
    "reply or inside a ```json fenced block):\n"
    '{"cosmo": "clarification", "question": "<what you need to know>"} '
    "- ask the orchestrator a question when the task is ambiguous.\n"
    '{"cosmo": "permission", "action": "<action>", "scope": {...}, '
    '"reason": "<why>"} - request approval before a sensitive action.\n'
    '{"cosmo": "error", "code": "<CODE>", "message": "<details>"} '
    "- report a structured failure.\n"
    '{"cosmo": "output", "output": {...}} - return a structured result.\n'
    "For a normal answer, just reply with plain text - do not wrap "
    "ordinary answers in a cosmo object."
)

# Sources whose Neuron wrapper accepts a ``system=`` kwarg.
#
# NOT groq / openrouter / together / mistral: those are aliases for
# ``_HuggingFaceNeuron`` pointed at an OpenAI-compatible base URL, and
# that wrapper takes its system prompt as a ``messages`` entry, not a
# ``system=`` kwarg. Listing them here made ``Axon.from_source()`` raise
# TypeError for all four.
_SYSTEM_CAPABLE_SOURCES = frozenset({
    "ollama", "openai", "anthropic",
})

_INTENT_KEY = "cosmo"
_FENCED_JSON = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_cosmo_intent(text: str) -> dict[str, Any] | None:
    """Return the ``cosmo`` intent object embedded in ``text``, or None.

    Inspects the whole trimmed string and any ```json fenced blocks. Only an
    object with a string ``cosmo`` key counts.
    """
    if not text:
        return None
    candidates: list[str] = [text.strip()]
    candidates.extend(m.group(1) for m in _FENCED_JSON.finditer(text))
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict) and isinstance(obj.get(_INTENT_KEY), str):
            return obj
    return None


def _intent_to_marker(intent: dict[str, Any]) -> dict[str, Any] | None:
    """Translate a ``cosmo`` intent object into an Axon marker dict."""
    kind = intent.get(_INTENT_KEY)
    if kind == "clarification":
        return {
            "__clarification__": True,
            "question": intent.get("question", ""),
            "context": intent.get("context"),
        }
    if kind == "permission":
        return {
            "__permission__": True,
            "action": intent.get("action", ""),
            "scope": intent.get("scope"),
            "reason": intent.get("reason"),
            "context": intent.get("context"),
        }
    if kind == "error":
        return {
            "__error__": True,
            "code": intent.get("code", "NEURON_ERROR"),
            "message": intent.get("message", ""),
            "recoverable": bool(intent.get("recoverable", False)),
        }
    if kind == "output":
        out = intent.get("output")
        return out if isinstance(out, dict) else {"value": out}
    return None


def _parse_llm_intents(raw: Any) -> dict[str, Any]:
    """Recogniser for LLM sources returning ``{"response": text, "meta": ...}``."""
    if not isinstance(raw, dict):
        return {"value": raw}
    text = raw.get("response")
    if isinstance(text, str):
        intent = _extract_cosmo_intent(text)
        if intent is not None:
            marker = _intent_to_marker(intent)
            if marker is not None:
                return marker
    return raw


def _parse_mcp_intents(raw: Any) -> dict[str, Any]:
    """Recogniser for the ``mcp`` source.

    ``is_error`` becomes an ERROR marker. Otherwise, if the tool's text
    response carries a ``cosmo`` intent it is honoured (an MCP server can drive
    clarification/permission too); else the result passes through as output.
    """
    if not isinstance(raw, dict):
        return {"value": raw}
    if raw.get("is_error"):
        msg = raw.get("response") or raw.get("content") or "MCP tool returned is_error"
        return {"__error__": True, "code": "MCP_TOOL_ERROR", "message": str(msg)}
    text = raw.get("response")
    if isinstance(text, str):
        intent = _extract_cosmo_intent(text)
        if intent is not None:
            marker = _intent_to_marker(intent)
            if marker is not None:
                return marker
    return raw
