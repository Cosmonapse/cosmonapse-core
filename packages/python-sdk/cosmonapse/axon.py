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

Lifecycle hooks (from cosmonapse._hooks.LifecycleHooks):
  @axon.on_connect          fires after the hosting Dendrite has emitted
                            REGISTER for this Axon
  @axon.on_refresh          fires on each heartbeat tick from the
                            hosting Dendrite (reason="heartbeat")
  @axon.on_schedule(every_s=N)  developer-supplied periodic task

Clarification convention
------------------------
If the agent returns a dict with `__clarification__: True`, the Axon
emits CLARIFICATION instead of AGENT_OUTPUT.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from cosmonapse._hooks import LifecycleHooks, RefreshEvent
from cosmonapse.engram.base import EngramBinding, EngramNotBound
from cosmonapse.envelope import (
    Signal,
    agent_output_signal,
    clarification_signal,
    error_signal,
)

if TYPE_CHECKING:
    from cosmonapse.dendrite import Dendrite

logger = logging.getLogger(__name__)


NeuronFn = Callable[[dict[str, Any], list[Any]], Awaitable[dict[str, Any]]]
ContextFetcher = Callable[[str], Awaitable[list[Any]]]


async def _noop_context_fetcher(ref: str) -> list[Any]:
    return []


class Axon(LifecycleHooks):
    """Agent-side tool that turns raw Neuron output into protocol-valid Signals."""

    def __init__(
        self,
        *,
        neuron_id: str,
        neuron_fn: NeuronFn,
        capabilities: list[str] | None = None,
        version: str | None = None,
        context_fetcher: ContextFetcher | None = None,
        engrams: list[EngramBinding] | None = None,
    ) -> None:
        LifecycleHooks.__init__(self)
        self.neuron_id = neuron_id
        self.capabilities = capabilities or []
        self.version = version
        self._fn = neuron_fn
        self._context_fetcher = context_fetcher or _noop_context_fetcher
        self._dendrite: "Dendrite | None" = None

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

        # Whether the wrapped neuron_fn declares recall/imprint kwargs.
        # Detected once at construction; cached for hot-path use.
        self._fn_accepts_recall: bool = False
        self._fn_accepts_imprint: bool = False
        self._fn_accepts_kwargs: bool = False
        try:
            import inspect as _inspect
            sig = _inspect.signature(neuron_fn)
            for _pname, _p in sig.parameters.items():
                if _p.kind is _inspect.Parameter.VAR_KEYWORD:
                    self._fn_accepts_kwargs = True
                    self._fn_accepts_recall = True
                    self._fn_accepts_imprint = True
                    break
                if _pname == "recall":
                    self._fn_accepts_recall = True
                if _pname == "imprint":
                    self._fn_accepts_imprint = True
        except (ValueError, TypeError):
            # Builtins / C functions have no inspectable signature. Skip
            # helper injection and fall back to the 2-arg legacy call.
            pass

    # -- attachment ----------------------------------------------------

    @property
    def dendrite(self) -> "Dendrite | None":
        return self._dendrite

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
        Fires on_connect hooks once, starts on_schedule loops."""
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
        """Run the Neuron and return AGENT_OUTPUT / CLARIFICATION / ERROR."""
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

        try:
            if kwargs:
                raw_output: dict[str, Any] = await self._fn(
                    input_data, context, **kwargs,
                )
            else:
                raw_output = await self._fn(input_data, context)
        except Exception as exc:
            logger.exception("Axon %s: Neuron raised", self.neuron_id)
            return error_signal(
                trace_id=trace_id,
                parent_id=parent_id,
                neuron=self.neuron_id,
                code="NEURON_EXCEPTION",
                message=str(exc),
                recoverable=False,
            )

        if isinstance(raw_output, dict) and raw_output.get("__clarification__"):
            return clarification_signal(
                trace_id=trace_id,
                parent_id=parent_id,
                neuron=self.neuron_id,
                question=raw_output.get("question", ""),
                context=raw_output.get("context"),
            )

        return agent_output_signal(
            trace_id=trace_id,
            parent_id=parent_id,
            neuron=self.neuron_id,
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
        ):
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
        ):
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
