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
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, cast

from cosmonapse._hooks import LifecycleHooks, RefreshEvent
from cosmonapse.effector.base import (
    EffectorBinding,
    EffectorError,
    EffectorNotBound,
)
from cosmonapse.effector.standards import TOOL_STANDARDS, extract_tool_call
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

    def __init__(self, axon: "Axon") -> None:
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
        output_parser: OutputParser | None = None,
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
        self._dendrite: "Dendrite | None" = None

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
        if effectors and self._tool_standard is None:
            raise ValueError(
                f"Axon {neuron_id!r}: effectors= requires tool_standard= "
                f"(one of {sorted(TOOL_STANDARDS)}) so the Axon can "
                f"recognise the Neuron's native tool calls"
            )
        self._effector_bindings: dict[str, EffectorBinding] = {}
        for eb in (effectors or []):
            if eb.name in self._effector_bindings:
                raise ValueError(
                    f"Axon {neuron_id!r}: duplicate EffectorBinding name "
                    f"{eb.name!r}"
                )
            self._effector_bindings[eb.name] = eb

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
        recognize: bool = True,
        teach_intents: bool | None = None,
        **source_kwargs: Any,
    ) -> "Axon":
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
        source accepts a ``system=`` kwarg (every LLM source except
        ``huggingface``; ``mcp`` is never taught). Pass False to opt out.
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
            output_parser=parser,
        )

    @classmethod
    def ollama(cls, neuron_id: str, **kw: Any) -> "Axon":
        """Axon paired with a local Ollama daemon. kwargs: ``model`` (required),
        ``endpoint``, ``system``, ``temperature``, ``max_tokens``, ``timeout``."""
        return cls.from_source("ollama", neuron_id=neuron_id, **kw)

    @classmethod
    def huggingface(cls, neuron_id: str, **kw: Any) -> "Axon":
        """Axon paired with a HuggingFace TGI / OpenAI-compatible endpoint.
        kwargs: ``endpoint`` (required), ``model``, ``use_chat_api``,
        ``temperature``, ``max_new_tokens``, ``api_key``, ``timeout``."""
        return cls.from_source("huggingface", neuron_id=neuron_id, **kw)

    # Alias matching the Neuron factory's ``"hf"``.
    hf = huggingface

    @classmethod
    def openai(cls, neuron_id: str, **kw: Any) -> "Axon":
        """Axon paired with the OpenAI Chat Completions API. kwargs: ``model``
        (required), ``api_key`` (or ``OPENAI_API_KEY``), ``endpoint``,
        ``temperature``, ``max_tokens``, ``system``, ``timeout``."""
        return cls.from_source("openai", neuron_id=neuron_id, **kw)

    @classmethod
    def anthropic(cls, neuron_id: str, **kw: Any) -> "Axon":
        """Axon paired with the Anthropic Messages API. kwargs: ``model``
        (required), ``api_key`` (or ``ANTHROPIC_API_KEY``), ``system``,
        ``max_tokens``, ``temperature``, ``timeout``."""
        return cls.from_source("anthropic", neuron_id=neuron_id, **kw)

    @classmethod
    def mcp(cls, neuron_id: str, **kw: Any) -> "Axon":
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

        The natural place for input normalisation (e.g. reshaping a
        re-dispatched clarification follow-up) or per-Axon policy checks.
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

    async def _apply_recognisers(self, raw: Any) -> Any:
        """Run registered detectors in precedence; return a marker dict on the
        first match, else the unchanged ``raw``."""
        rec = self._recognisers
        if not any(rec.values()):
            return raw

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
            return {"__error__": True, **hit}
        hit = await _first(rec["clarification"])
        if hit is not None:
            return {"__clarification__": True, **hit}
        hit = await _first(rec["permission"])
        if hit is not None:
            return {"__permission__": True, **hit}
        hit = await _first(rec["output"])
        if hit is not None:
            return hit
        return raw

    # -- attachment ----------------------------------------------------

    @property
    def dendrite(self) -> "Dendrite | None":
        return self._dendrite

    @property
    def host(self) -> _HostProxy:
        """Deferred Dendrite decorators - see :class:`_HostProxy`."""
        return _HostProxy(self)

    def attach_to(self, dendrite: "Dendrite") -> None:
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
            for name, st, filters, fn in self._host_regs:
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
            except Exception:  # noqa: BLE001  -  teardown must not raise
                logger.warning("Axon %s: neuron aclose() failed", self.neuron_id, exc_info=True)

    # -- core: handle one TASK ----------------------------------------

    async def handle_task(self, task: Signal) -> Signal:
        """Run the Neuron and return AGENT_OUTPUT / CLARIFICATION / ERROR.

        Binds the TASK's (trace_id, parent_id=task.id) as the ambient trace
        context for the whole handling pass - neuron_fn, detectors, and
        lifecycle hooks included - so engram calls made without explicit
        trace plumbing (e.g. ``dendrite.imprint`` from a
        ``@detects_output`` hook) are attributed to this task's trace.
        """
        with trace_context(task.trace_id, task.id):
            return await self._handle_task_inner(task)

    async def _handle_task_inner(self, task: Signal) -> Signal:
        trace_id = task.trace_id
        parent_id = task.id
        input_data: dict[str, Any] = task.payload.get("input", {})
        context_ref: str | None = task.payload.get("context_ref")

        context: list[Any] = []
        if context_ref:
            try:
                context = await self._context_fetcher(context_ref)
            except Exception as exc:
                logger.warning(
                    "Axon %s: context fetch failed for %r: %s",
                    self.neuron_id, context_ref, exc,
                )

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

        try:
            if self._before_task_hooks:
                input_data = await self._apply_before_task(input_data)
            if kwargs:
                raw_output: dict[str, Any] = await self._fn(
                    input_data, context, **kwargs,
                )
            else:
                raw_output = await self._fn(input_data, context)
            # Native tool-call recognition (tool_standard=). The model
            # spoke its trained dialect; a match takes precedence over
            # the cosmo parser and recognisers - a tool call IS the
            # intent, there is nothing further to recognise.
            native_call: dict[str, Any] | None = None
            if self._tool_standard is not None:
                native_call = extract_tool_call(
                    raw_output, self._tool_standard,
                )
            if native_call is None:
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
                raw_output = await self._apply_recognisers(raw_output)
        except Exception as exc:
            logger.exception("Axon %s: Neuron raised", self.neuron_id)
            return error_signal(
                trace_id=trace_id,
                parent_id=parent_id,
                directed=Directed(id=self.neuron_id),
                code="NEURON_EXCEPTION",
                message=str(exc),
                recoverable=False,
            )

        # Native tool call: translate-and-act. With a serving binding the
        # Axon dispatches through the EffectorClient and the AGENT_OUTPUT
        # carries the observation; with no bindings at all it carries the
        # translated call for the host chain to execute (pure translation).
        if native_call is not None:
            return await self._dispatch_native_tool_call(
                native_call, trace_id=trace_id, parent_id=parent_id,
            )

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
            )

        if isinstance(raw_output, dict) and raw_output.get("__clarification__"):
            return clarification_signal(
                trace_id=trace_id,
                parent_id=parent_id,
                directed=Directed(id=self.neuron_id),
                question=raw_output.get("question", ""),
                context=raw_output.get("context"),
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
            )

        return agent_output_signal(
            trace_id=trace_id,
            parent_id=parent_id,
            directed=Directed(id=self.neuron_id),
            output=raw_output if isinstance(raw_output, dict) else {"value": raw_output},
        )


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

    async def _dispatch_native_tool_call(
        self,
        call: dict[str, Any],
        *,
        trace_id: str,
        parent_id: str,
    ) -> Signal:
        """Act on a recognised native tool call and wrap the observation.

        Always returns AGENT_OUTPUT: a tool failure (timeout, tool-level
        error, no serving binding) rides ``error`` in the output payload
        for the Neuron/host to react to - it never terminates the TASK.
        With no bindings declared the translated call passes through
        unexecuted (``{"tool", "args", "call_id"}``) for the host
        chain to run - the pre-binding harness pattern, minus the
        hand-written parser.
        """
        tool = call["tool"]
        args = call.get("args") or {}
        call_id = call.get("call_id")
        out: dict[str, Any] = {"tool": tool, "args": args}
        if call_id is not None:
            out["call_id"] = call_id

        if not self._effector_bindings:
            return agent_output_signal(
                trace_id=trace_id,
                parent_id=parent_id,
                directed=Directed(id=self.neuron_id),
                output=out,
            )

        binding = self._resolve_binding_for_tool(tool)
        if binding is None:
            out["error"] = (
                f"no effector binding serves tool {tool!r}; "
                f"bound: {sorted(self._effector_bindings)}"
            )
            return agent_output_signal(
                trace_id=trace_id,
                parent_id=parent_id,
                directed=Directed(id=self.neuron_id),
                output=out,
            )

        try:
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
        except EffectorError as exc:
            out["error"] = f"{type(exc).__name__}: {exc}"
        except Exception as exc:  # noqa: BLE001 - tools never kill TASKs
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
        return agent_output_signal(
            trace_id=trace_id,
            parent_id=parent_id,
            directed=Directed(id=self.neuron_id),
            output=out,
        )

    @property
    def effector_bindings(self) -> dict[str, EffectorBinding]:
        return dict(self._effector_bindings)

    @property
    def tool_standard(self) -> str | None:
        return self._tool_standard


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
_SYSTEM_CAPABLE_SOURCES = frozenset({
    "ollama", "openai", "anthropic", "groq", "openrouter", "together",
    "mistral",
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
