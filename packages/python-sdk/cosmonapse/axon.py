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
    ) -> None:
        LifecycleHooks.__init__(self)
        self.neuron_id = neuron_id
        self.capabilities = capabilities or []
        self.version = version
        self._fn = neuron_fn
        self._context_fetcher = context_fetcher or _noop_context_fetcher
        self._dendrite: "Dendrite | None" = None

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
            except Exception:  # noqa: BLE001 — teardown must not raise
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

        try:
            raw_output: dict[str, Any] = await self._fn(input_data, context)
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
