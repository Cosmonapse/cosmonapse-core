"""
cosmonapse.dendrite
~~~~~~~~~~~~~~~~~~~
The Dendrite is the synapse-side participant.

Orchestration: there is no separate Cortex class. Every Dendrite has
`dispatch_task`, `emit_final`, `emit_error`, `emit`, and the inbound-
handler decorators. A Cortex is just a Dendrite that uses them. The
`Cortex` symbol is kept as a back-compat alias.

Cognition surface
-----------------
Every cognition signal type has a first-class emit helper and a
matching @on_* decorator on this class. All decorators accept the
optional filter kwargs ``neuron=`` / ``capability=`` / ``trace_id=``
so a handler can be scoped without manual filtering inside the body.
``on_trace(trace_id, *types)`` registers one handler for multiple
types narrowed to a single workflow.

Close-the-loop helpers
----------------------
``respond_to_clarification(sig, answer=...)`` and
``respond_to_escalation(sig, neuron=...)`` re-dispatch a TASK that
preserves the trace lineage (``parent_id`` -> the prompting signal,
``trace_id`` carried over) so the workflow continues on the same
conversation thread.
"""

from __future__ import annotations

import asyncio
import logging
import random
import warnings
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from cosmonapse._hooks import LifecycleHooks, RefreshEvent
from cosmonapse.axon import Axon
from cosmonapse.engram.base import Engram
from cosmonapse.engram.client import EngramClient
from cosmonapse.envelope import (
    AXON_TYPES,
    SYNAPSE_TYPES,
    Signal,
    SignalType,
    bid_signal,
    clarification_answer_signal,
    consensus_signal,
    context_sync_signal,
    critique_signal,
    deregister_signal,
    discover_signal,
    error_signal,
    escalation_signal,
    final_signal,
    heartbeat_signal,
    imprinted_signal,
    memory_append_signal,
    new_trace_id,
    permission_decision_signal,
    plan_signal,
    recalled_signal,
    register_signal,
    task_awarded_signal,
    task_declined_signal,
    task_offer_signal,
    task_signal,
    thought_delta_signal,
    tool_call_signal,
    tool_result_signal,
)
from cosmonapse.pathway import PATHWAY_TYPES, Pathway
from cosmonapse.storage.base import NeuronRecord, RegistryStore
from cosmonapse.synapse.base import MessageHandler, Subscription, Synapse

logger = logging.getLogger(__name__)


SignalHandler = Callable[[Signal], Awaitable[None]]


class DendriteProtocolError(ValueError):
    """Raised when an emit violates the protocol (e.g. emitting an AXON-only type)."""


# Back-compat aliases.
CortexProtocolError = DendriteProtocolError


class Dendrite(LifecycleHooks):
    """Synapse-side participant. Synapse required, everything else optional."""

    def __init__(
        self,
        *,
        synapse: Synapse,
        registry_store: RegistryStore | None = None,
        namespace: str = "default",
        dendrite_id: str = "dendrite",
        heartbeat_s: float = 30.0,
        reregister_on_heartbeat: bool = True,
        role: str = "orchestrator",
    ) -> None:
        if synapse is None:
            raise TypeError("Dendrite requires a synapse (Synapse)")
        if role not in ("orchestrator", "worker"):
            raise ValueError(
                f"role must be 'orchestrator' or 'worker', got {role!r}"
            )
        LifecycleHooks.__init__(self)

        self._synapse = synapse
        self._registry_store = registry_store
        self._namespace = namespace
        self.dendrite_id = dendrite_id
        self._heartbeat_s = heartbeat_s
        self._reregister_on_heartbeat = reregister_on_heartbeat
        self._role = role

        self._axons: dict[str, Axon] = {}
        # Handlers are keyed by every SignalType (not just AXON_TYPES) so
        # cognition decorators (@on_plan, @on_critique, ...) can attach
        # without an init-time KeyError. Dispatch for non-AXON types still
        # only fires when a handler is registered.
        self._handlers: dict[SignalType, list[SignalHandler]] = {
            t: [] for t in SignalType
        }
        self._discover_handlers: list[SignalHandler] = []

        self._task_sub: Subscription | None = None
        self._routed_task_sub: Subscription | None = None
        self._inbound_subs: dict[SignalType, Subscription] = {}
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._running = False

        # Open Pathways keyed by trace_id. Populated by dispatch() and
        # observe_pathway(); evicted on Pathway.close() via _on_pathway_close.
        self._pathways: dict[str, Pathway] = {}

        # Attached Engrams keyed by engram_id. Routing also indexes by
        # engram_kind so RECALL/IMPRINT with engram_kind= addresses all
        # matching hosts (typically one per kind by deployment convention).
        # The EngramClient owns the caller-side correlation table for
        # in-flight RECALL/IMPRINT awaiting RECALLED/IMPRINTED.
        self._engrams: dict[str, Engram] = {}
        self._engram_kind_index: dict[str, list[str]] = {}
        self._engram_client: EngramClient = EngramClient(self)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def synapse(self) -> Synapse:
        return self._synapse

    @property
    def registry_store(self) -> RegistryStore | None:
        return self._registry_store

    @property
    def namespace(self) -> str:
        return self._namespace

    @property
    def axons(self) -> dict[str, Axon]:
        return dict(self._axons)

    def axon(self, neuron_id: str) -> Axon | None:
        return self._axons.get(neuron_id)

    @property
    def role(self) -> str:
        """'orchestrator' (can dispatch) or 'worker' (hosts Axons only)."""
        return self._role

    @property
    def capabilities(self) -> list[str]:
        """Aggregate of every attached Axon's capabilities, deduplicated and
        sorted for deterministic ordering.

        Per the cosmonapse design, capabilities are conceptually owned by
        the Dendrite (it is the routing decision-maker); each Axon declares
        the specific capabilities its Neuron provides, and the Dendrite
        exposes the union. Used by capability-routed dispatch to compute
        queue-group names and filter inbound TASKs.
        """
        caps: set[str] = set()
        for ax in self._axons.values():
            caps.update(ax.capabilities)
        return sorted(caps)

    def _cap_queue_group(self) -> str | None:
        """Canonical queue-group name for this Dendrite's aggregate caps.
        Returns None when no Axons are attached. Identical Dendrites
        (same aggregate cap set) share a group and load-balance."""
        caps = self.capabilities
        if not caps:
            return None
        return "caps:" + ",".join(caps)

    def _require_orchestrator(self, op: str) -> None:
        """Guard for dispatch-side methods. Workers cannot emit TASK."""
        if self._role != "orchestrator":
            raise DendriteProtocolError(
                f"Dendrite role={self._role!r} cannot perform {op!r}: "
                f"only role='orchestrator' Dendrites may dispatch TASK "
                f"signals. Workers host Axons and emit AGENT_OUTPUT / "
                f"CLARIFICATION / ERROR only."
            )

    # ------------------------------------------------------------------
    # Attachment
    # ------------------------------------------------------------------

    def attach_axon(self, axon: Axon) -> None:
        if axon.neuron_id in self._axons:
            raise ValueError(
                f"Dendrite already has an Axon for neuron_id={axon.neuron_id!r}"
            )
        self._axons[axon.neuron_id] = axon
        axon.attach_to(self)

    def attach_engram(self, engram: Engram) -> None:
        """Mount an Engram on this Dendrite.

        After attachment, the Dendrite subscribes to RECALL/IMPRINT
        signals addressed to ``engram.engram_id`` or matching
        ``engram.engram_kind`` and dispatches them to the attached
        instance. The Engram still owns its backend lifecycle  - 
        ``connect()`` is called on Dendrite.start() and ``close()`` on
        Dendrite.stop().

        Multiple Engrams may share an ``engram_kind``; addressed routing
        by ``engram_id`` still works because the receiving Dendrite
        filters before dispatch.
        """
        if engram.engram_id in self._engrams:
            raise ValueError(
                f"Dendrite already hosts an Engram with engram_id="
                f"{engram.engram_id!r}"
            )
        self._engrams[engram.engram_id] = engram
        self._engram_kind_index.setdefault(engram.engram_kind, []).append(
            engram.engram_id
        )

    async def detach_engram(self, engram_id: str) -> None:
        """Remove a hosted Engram. Closes its backend if the Dendrite
        is running."""
        engram = self._engrams.get(engram_id)
        if engram is None:
            raise KeyError(
                f"Dendrite has no Engram with engram_id={engram_id!r}"
            )
        if self._running:
            try:
                await engram.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Dendrite: Engram %s close raised on detach: %s",
                    engram_id, exc,
                )
        bucket = self._engram_kind_index.get(engram.engram_kind, [])
        if engram_id in bucket:
            bucket.remove(engram_id)
        if not bucket:
            self._engram_kind_index.pop(engram.engram_kind, None)
        del self._engrams[engram_id]

    @property
    def engrams(self) -> dict[str, Engram]:
        return dict(self._engrams)

    async def detach_axon(self, neuron_id: str, *,
                          reason: str | None = None) -> None:
        axon = self._axons.get(neuron_id)
        if axon is None:
            raise KeyError(
                f"Dendrite has no Axon for neuron_id={neuron_id!r}"
            )

        if self._running:
            try:
                await axon._on_deregister_emitted()
            except Exception as exc:
                logger.warning("Axon teardown raised: %s", exc)
            if self._registry_store is not None:
                try:
                    await self._registry_store.mark_deregistered(neuron_id)
                except Exception as exc:
                    logger.warning(
                        "Dendrite: store mark_deregistered failed: %s", exc
                    )
            await self._emit_deregister(axon, reason=reason)

        del self._axons[neuron_id]
        axon.detach()

        if self._running and not self._axons:
            if self._task_sub is not None:
                try:
                    await self._task_sub.unsubscribe()
                except Exception as exc:
                    logger.warning(
                        "Dendrite failed to unsubscribe TASK: %s", exc,
                    )
                self._task_sub = None
            if self._routed_task_sub is not None:
                try:
                    await self._routed_task_sub.unsubscribe()
                except Exception as exc:
                    logger.warning(
                        "Dendrite failed to unsubscribe routed TASK: %s", exc,
                    )
                self._routed_task_sub = None

    # ------------------------------------------------------------------
    # Handler registration with filter support
    # ------------------------------------------------------------------

    def _wrap_with_filter(
        self,
        fn: SignalHandler,
        *,
        neuron: str | None,
        capability: str | None,
        trace_id: str | None,
    ) -> SignalHandler:
        if neuron is None and capability is None and trace_id is None:
            return fn

        async def filtered(sig: Signal) -> None:
            if neuron is not None and sig.neuron != neuron:
                return
            if trace_id is not None and sig.trace_id != trace_id:
                return
            if capability is not None:
                if not await self._neuron_has_capability(sig.neuron, capability):
                    return
            await fn(sig)

        filtered.__wrapped__ = fn  # type: ignore[attr-defined]
        filtered.__cosmonapse_filter__ = {  # type: ignore[attr-defined]
            "neuron": neuron, "capability": capability, "trace_id": trace_id,
        }
        return filtered

    async def _neuron_has_capability(
        self, neuron_id: str | None, capability: str,
    ) -> bool:
        if not neuron_id:
            return False
        axon = self._axons.get(neuron_id)
        if axon is not None:
            return capability in axon.capabilities
        if self._registry_store is not None:
            try:
                rec = await self._registry_store.get(neuron_id)
            except Exception:
                return False
            if rec is None:
                return False
            return capability in (rec.capabilities or [])
        return False

    def _on(
        self,
        signal_type: SignalType,
        *,
        neuron: str | None = None,
        capability: str | None = None,
        trace_id: str | None = None,
    ) -> Callable[[SignalHandler], SignalHandler]:
        def decorator(fn: SignalHandler) -> SignalHandler:
            handler = self._wrap_with_filter(
                fn, neuron=neuron, capability=capability, trace_id=trace_id,
            )
            self._handlers[signal_type].append(handler)
            if self._running and signal_type not in self._inbound_subs:
                asyncio.create_task(self._ensure_inbound_sub(signal_type))
            return fn
        return decorator

    @staticmethod
    def _decorator_or_call(
        fn: SignalHandler | None,
        decorator: Callable[[SignalHandler], SignalHandler],
    ) -> Any:
        if fn is None:
            return decorator
        return decorator(fn)

    # -- Lifecycle decorators --------------------------------------------

    def on_agent_output(self, fn: SignalHandler | None = None, *, neuron: str | None = None, capability: str | None = None, trace_id: str | None = None) -> Any:
        return self._decorator_or_call(fn, self._on(
            SignalType.AGENT_OUTPUT,
            neuron=neuron, capability=capability, trace_id=trace_id,
        ))

    def on_clarification(self, fn: SignalHandler | None = None, *, neuron: str | None = None, capability: str | None = None, trace_id: str | None = None) -> Any:
        return self._decorator_or_call(fn, self._on(
            SignalType.CLARIFICATION,
            neuron=neuron, capability=capability, trace_id=trace_id,
        ))

    def on_permission(self, fn: SignalHandler | None = None, *, neuron: str | None = None, capability: str | None = None, trace_id: str | None = None) -> Any:
        """Register a handler fired on inbound PERMISSION requests.

        The handler is the *answering* side: a central Cortex or a peer
        Dendrite evaluates the request (often consulting an Engram of
        standing grants, keyed per-neuron) and replies via
        :meth:`respond_to_permission` (re-dispatch a TASK carrying the
        verdict) or :meth:`grant_permission` / :meth:`deny_permission`
        (emit a discrete PERMISSION_DECISION). It may also imprint the
        decision into an Engram so future RECALLs hit.
        """
        return self._decorator_or_call(fn, self._on(
            SignalType.PERMISSION,
            neuron=neuron, capability=capability, trace_id=trace_id,
        ))

    def on_error_signal(self, fn: SignalHandler | None = None, *, neuron: str | None = None, capability: str | None = None, trace_id: str | None = None) -> Any:
        return self._decorator_or_call(fn, self._on(
            SignalType.ERROR,
            neuron=neuron, capability=capability, trace_id=trace_id,
        ))

    def on_register_signal(self, fn: SignalHandler | None = None, *, neuron: str | None = None, capability: str | None = None, trace_id: str | None = None) -> Any:
        return self._decorator_or_call(fn, self._on(
            SignalType.REGISTER,
            neuron=neuron, capability=capability, trace_id=trace_id,
        ))

    def on_deregister_signal(self, fn: SignalHandler | None = None, *, neuron: str | None = None, capability: str | None = None, trace_id: str | None = None) -> Any:
        return self._decorator_or_call(fn, self._on(
            SignalType.DEREGISTER,
            neuron=neuron, capability=capability, trace_id=trace_id,
        ))

    def on_heartbeat_signal(self, fn: SignalHandler | None = None, *, neuron: str | None = None, capability: str | None = None, trace_id: str | None = None) -> Any:
        return self._decorator_or_call(fn, self._on(
            SignalType.HEARTBEAT,
            neuron=neuron, capability=capability, trace_id=trace_id,
        ))

    # -- Cognition decorators --------------------------------------------

    def on_plan(self, fn: SignalHandler | None = None, *, neuron: str | None = None, capability: str | None = None, trace_id: str | None = None) -> Any:
        return self._decorator_or_call(fn, self._on(
            SignalType.PLAN,
            neuron=neuron, capability=capability, trace_id=trace_id,
        ))

    def on_thought_delta(self, fn: SignalHandler | None = None, *, neuron: str | None = None, capability: str | None = None, trace_id: str | None = None) -> Any:
        return self._decorator_or_call(fn, self._on(
            SignalType.THOUGHT_DELTA,
            neuron=neuron, capability=capability, trace_id=trace_id,
        ))

    def on_tool_call(self, fn: SignalHandler | None = None, *, neuron: str | None = None, capability: str | None = None, trace_id: str | None = None) -> Any:
        return self._decorator_or_call(fn, self._on(
            SignalType.TOOL_CALL,
            neuron=neuron, capability=capability, trace_id=trace_id,
        ))

    def on_tool_result(self, fn: SignalHandler | None = None, *, neuron: str | None = None, capability: str | None = None, trace_id: str | None = None) -> Any:
        return self._decorator_or_call(fn, self._on(
            SignalType.TOOL_RESULT,
            neuron=neuron, capability=capability, trace_id=trace_id,
        ))

    def on_memory_append(self, fn: SignalHandler | None = None, *, neuron: str | None = None, capability: str | None = None, trace_id: str | None = None) -> Any:
        return self._decorator_or_call(fn, self._on(
            SignalType.MEMORY_APPEND,
            neuron=neuron, capability=capability, trace_id=trace_id,
        ))

    def on_critique(self, fn: SignalHandler | None = None, *, neuron: str | None = None, capability: str | None = None, trace_id: str | None = None) -> Any:
        return self._decorator_or_call(fn, self._on(
            SignalType.CRITIQUE,
            neuron=neuron, capability=capability, trace_id=trace_id,
        ))

    def on_escalation(self, fn: SignalHandler | None = None, *, neuron: str | None = None, capability: str | None = None, trace_id: str | None = None) -> Any:
        return self._decorator_or_call(fn, self._on(
            SignalType.ESCALATION,
            neuron=neuron, capability=capability, trace_id=trace_id,
        ))

    def on_consensus(self, fn: SignalHandler | None = None, *, neuron: str | None = None, capability: str | None = None, trace_id: str | None = None) -> Any:
        return self._decorator_or_call(fn, self._on(
            SignalType.CONSENSUS,
            neuron=neuron, capability=capability, trace_id=trace_id,
        ))

    def on_context_sync(self, fn: SignalHandler | None = None, *, neuron: str | None = None, capability: str | None = None, trace_id: str | None = None) -> Any:
        return self._decorator_or_call(fn, self._on(
            SignalType.CONTEXT_SYNC,
            neuron=neuron, capability=capability, trace_id=trace_id,
        ))

    # -- Trace-scoped handler --------------------------------------------

    _TRACE_DEFAULT_TYPES: tuple[SignalType, ...] = (
        SignalType.AGENT_OUTPUT,
        SignalType.FINAL,
        SignalType.ERROR,
        SignalType.PLAN,
        SignalType.THOUGHT_DELTA,
        SignalType.TOOL_CALL,
        SignalType.TOOL_RESULT,
        SignalType.MEMORY_APPEND,
        SignalType.CRITIQUE,
        SignalType.ESCALATION,
        SignalType.CONSENSUS,
        SignalType.CONTEXT_SYNC,
        SignalType.CLARIFICATION,
    )

    def on_trace(
        self,
        trace_id: str,
        *types: SignalType,
        neuron: str | None = None,
        capability: str | None = None,
    ) -> Callable[[SignalHandler], SignalHandler]:
        signal_types = types or self._TRACE_DEFAULT_TYPES

        def decorator(fn: SignalHandler) -> SignalHandler:
            for t in signal_types:
                self._on(
                    t, neuron=neuron, capability=capability, trace_id=trace_id,
                )(fn)
            return fn
        return decorator

    def on_discover(self, fn: SignalHandler) -> SignalHandler:
        self._discover_handlers.append(fn)
        if self._running and SignalType.DISCOVER not in self._inbound_subs:
            asyncio.create_task(self._ensure_inbound_sub(SignalType.DISCOVER))
        return fn

    # -- Deprecated short aliases ----------------------------------------

    def _deprecated_alias(self, old: str, new: str) -> None:
        warnings.warn(
            f"Dendrite.{old} is deprecated and will be removed in a future "
            f"release; use Dendrite.{new} instead.",
            DeprecationWarning,
            stacklevel=3,
        )

    def on_error(self, fn: SignalHandler | None = None) -> Any:
        self._deprecated_alias("on_error", "on_error_signal")
        return self.on_error_signal(fn)

    def on_register(self, fn: SignalHandler | None = None) -> Any:
        self._deprecated_alias("on_register", "on_register_signal")
        return self.on_register_signal(fn)

    def on_deregister(self, fn: SignalHandler | None = None) -> Any:
        self._deprecated_alias("on_deregister", "on_deregister_signal")
        return self.on_deregister_signal(fn)

    def on_heartbeat(self, fn: SignalHandler | None = None) -> Any:
        self._deprecated_alias("on_heartbeat", "on_heartbeat_signal")
        return self.on_heartbeat_signal(fn)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return

        await self._synapse.connect()

        if self._registry_store is not None:
            await self._registry_store.connect()

        if self._axons:
            # Two TASK subscriptions for two routing modes:
            #
            # 1) ADDRESSED  -  broadcast on ``cosmonapse.<ns>.TASK``, no
            #    queue group. Every Dendrite gets every addressed TASK;
            #    only the one hosting the named Axon acts. Putting a
            #    queue group here would break addressed routing  -  the
            #    broker could deliver to a Dendrite that doesn't host
            #    the target, and the TASK would be silently dropped.
            #
            # 2) ROUTED  -  capability-routed on ``cosmonapse.<ns>.TASK.routed``,
            #    queue_group keyed on this Dendrite's aggregate cap
            #    signature. Identical Dendrites (same Axon cap profile)
            #    share a group and load-balance, so a capability-routed
            #    TASK is consumed exactly once within the group. This is
            #    the "dead after one consume" delivery semantic.
            #
            # Heterogeneous case (different Dendrites with different but
            # overlapping cap profiles) still gets at-least-once across
            # groups; use TASK_OFFER / BID for atomic claim across that
            # population.
            self._task_sub = await self._synapse.subscribe(
                self._subject(SignalType.TASK),
                self._on_task,
            )
            qgroup = self._cap_queue_group()
            if qgroup is not None:
                self._routed_task_sub = await self._synapse.subscribe(
                    self._routed_subject(),
                    self._on_task,
                    queue_group=qgroup,
                )
            # Hosting Axons means we might be the winner of a
            # TASK_AWARDED. Subscribe to TASK_AWARDED with our routing
            # handler; the handler treats matching awards as TASKs.
            await self._ensure_inbound_sub(SignalType.TASK_AWARDED)
            await self._ensure_inbound_sub(SignalType.DISCOVER)
            for axon in self._axons.values():
                await self._mirror_to_store(axon, status="registered")
                await self._emit_register(axon)
                await axon._on_register_emitted()

        # Engram subscriptions. A Dendrite hosting Engrams listens for
        # RECALL/IMPRINT and routes them; any Dendrite that may issue
        # RECALL/IMPRINT (via recall/imprint helpers, or because it
        # hosts Axons whose Neurons use the injected helpers) listens
        # for RECALLED/IMPRINTED to correlate responses by parent_id.
        if self._engrams:
            for engram in self._engrams.values():
                try:
                    await engram.connect()
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "Dendrite: Engram %s connect failed: %s",
                        engram.engram_id, exc,
                    )
            await self._ensure_inbound_sub(SignalType.RECALL)
            await self._ensure_inbound_sub(SignalType.IMPRINT)
        # Always listen for RECALLED/IMPRINTED  -  the Dendrite owns the
        # EngramClient's correlation table even when it hosts no Axons,
        # because a Cortex calls dendrite.recall/imprint directly.
        await self._ensure_inbound_sub(SignalType.RECALLED)
        await self._ensure_inbound_sub(SignalType.IMPRINTED)

        for signal_type, handlers in self._handlers.items():
            if handlers:
                await self._ensure_inbound_sub(signal_type)

        if self._registry_store is not None:
            for mgmt_type in (SignalType.REGISTER, SignalType.DEREGISTER,
                              SignalType.HEARTBEAT):
                await self._ensure_inbound_sub(mgmt_type)

        self._running = True

        if self._axons and self._heartbeat_s > 0:
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        if self._registry_store is not None:
            try:
                await self._emit_discover()
            except Exception as exc:
                logger.warning("Dendrite: initial DISCOVER emit failed: %s", exc)

        self._launch_schedule()
        await self._fire_connect()

        logger.info(
            "Dendrite %s started on namespace %r (axons=%d, inbound_subs=%d)",
            self.dendrite_id, self._namespace,
            len(self._axons), len(self._inbound_subs),
        )

    async def stop(self, reason: str | None = None) -> None:
        # Close any open Pathways FIRST so awaiters don't hang and
        # ``async for`` loops see the close sentinel cleanly. Each
        # Pathway's ``on_close`` callback evicts it from ``_pathways``;
        # iterate over a snapshot to avoid mutating-during-iteration.
        for pathway in list(self._pathways.values()):
            try:
                await pathway.close()
            except Exception as exc:
                logger.warning("Pathway close raised: %s", exc)
        self._pathways.clear()

        if not self._running:
            return
        self._running = False

        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

        if self._task_sub is not None:
            await self._task_sub.unsubscribe()
            self._task_sub = None

        if self._routed_task_sub is not None:
            await self._routed_task_sub.unsubscribe()
            self._routed_task_sub = None

        for sub in list(self._inbound_subs.values()):
            try:
                await sub.unsubscribe()
            except Exception as exc:
                logger.warning("Dendrite failed to unsubscribe inbound: %s", exc)
        self._inbound_subs.clear()

        # Cancel any in-flight engram I/O  -  Futures resolve with
        # EngramCancelled so awaiters get a clean exception instead of
        # hanging on the deadline.
        try:
            self._engram_client.cancel_all()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Dendrite: EngramClient cancel_all raised: %s", exc)

        for engram in self._engrams.values():
            try:
                await engram.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Dendrite: Engram %s close raised on stop: %s",
                    engram.engram_id, exc,
                )

        for axon in self._axons.values():
            try:
                await axon._on_deregister_emitted()
            except Exception as exc:
                logger.warning("Axon teardown raised: %s", exc)
            if self._registry_store is not None:
                try:
                    await self._registry_store.mark_deregistered(axon.neuron_id)
                except Exception as exc:
                    logger.warning("Dendrite: store mark_deregistered failed: %s", exc)
            await self._emit_deregister(axon, reason=reason)

        await self._stop_hooks()

        logger.info("Dendrite %s stopped (namespace=%r)",
                    self.dendrite_id, self._namespace)

    async def __aenter__(self) -> "Dendrite":
        await self.start()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.stop()

    # ------------------------------------------------------------------
    # Registry helpers
    # ------------------------------------------------------------------

    def _require_store(self) -> RegistryStore:
        if self._registry_store is None:
            raise RuntimeError(
                "Dendrite has no registry_store - pass one at construction "
                "to use registry helpers (find_neurons / registry_snapshot)."
            )
        return self._registry_store

    async def registry_snapshot(
        self, *, capability: str | None = None,
        include_deregistered: bool = False,
    ) -> list[NeuronRecord]:
        return await self._require_store().list(
            capability=capability,
            include_deregistered=include_deregistered,
        )

    async def find_neurons(self, *, capability: str | None = None) -> list[NeuronRecord]:
        return await self._require_store().list(
            capability=capability,
            include_deregistered=False,
        )

    # ------------------------------------------------------------------
    # Outbound primitives
    # ------------------------------------------------------------------

    async def dispatch_task(
        self, *, neuron: str | None = None, input: dict[str, Any],
        trace_id: str | None = None, parent_id: str | None = None,
        context_ref: str | None = None,
        capabilities: list[str] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Signal:
        """Emit a TASK signal. Addressed (``neuron=...``) or capability-routed
        (``capabilities=[...]``)  -  at least one must be set.

        Addressed TASKs go on the broadcast TASK subject; the unique
        host filters by neuron_id and acts. Capability-routed TASKs go
        on the queue-grouped routed subject so the broker delivers them
        to exactly one Dendrite per matching cap profile.

        Only orchestrator-role Dendrites may dispatch.
        """
        self._require_orchestrator("dispatch_task")
        if neuron is None and not capabilities:
            raise ValueError(
                "dispatch_task requires either neuron= (addressed) or "
                "capabilities=[...] (capability-routed)"
            )
        sig = task_signal(
            trace_id=trace_id, parent_id=parent_id, neuron=neuron,
            input=input, context_ref=context_ref,
            capabilities=capabilities, meta=meta,
        )
        await self._publish_task(sig)
        return sig

    async def _publish_task(self, sig: Signal) -> None:
        """Publish a TASK to the correct subject for its routing mode.

        Addressed (``sig.neuron`` set) → broadcast subject.
        Capability-routed (no neuron, capabilities in payload) → routed
        subject (queue-grouped on receivers, once-only delivery within
        a matching cap profile).
        """
        if sig.neuron:
            subject = self._subject(SignalType.TASK)
        elif sig.payload.get("capabilities"):
            subject = self._routed_subject()
        else:
            subject = self._subject(SignalType.TASK)
        await self._synapse.publish(subject, sig)

    # -- Pathway-based dispatch (opt-in) ---------------------------------
    # ``dispatch_task`` above is fire-and-forget: it returns the emitted
    # TASK Signal and leaves response correlation to the caller. The two
    # methods below open a :class:`Pathway` scoped to the trace so the
    # caller can ``await`` the reply, attach trace-scoped handlers, or
    # iterate the stream - whichever shape fits the workflow.
    # ``observe_pathway`` is the matching primitive for the decentralised
    # case: watch a trace started by another peer without emitting a TASK.

    async def dispatch(
        self,
        *,
        neuron: str | None = None,
        input: dict[str, Any],
        trace_id: str | None = None,
        parent_id: str | None = None,
        context_ref: str | None = None,
        capabilities: list[str] | None = None,
        meta: dict[str, Any] | None = None,
        scope: str = "all",
    ) -> Pathway:
        """Dispatch a TASK and return a :class:`Pathway` scoped to its trace.

        Unlike :meth:`dispatch_task` (which returns the emitted Signal),
        ``dispatch`` returns a Pathway you can await, iterate, or attach
        trace-scoped handlers to. Three consumption shapes on the same
        primitive - the dev picks whichever fits the workflow::

            # 1) sequential / request-reply
            pw = await orch.dispatch(neuron="summarize", input={...})
            out = await pw.wait()

            # 2) reactive
            pw = await orch.dispatch(neuron="planner", input={...})
            @pw.on(SignalType.PLAN)
            async def show_plan(sig): ...

            # 3) streaming
            async with await orch.dispatch(neuron="agent", input={...}) as pw:
                async for sig in pw:
                    ...

        Pass ``trace_id=`` and ``parent_id=`` to continue an existing
        trace (decentralised case: pick up a workflow another peer
        started).

        Pass ``capabilities=[...]`` instead of ``neuron=`` for event-driven
        dispatch: any Dendrite whose attached Axons cover the requested
        capability set may pick it up.

        ``scope="all"`` (default) delivers every PATHWAY_TYPES Signal on
        the trace to the Pathway; ``scope="terminal"`` filters to FINAL /
        ERROR / CLARIFICATION only  -  the decentralised pattern where
        intermediate orchestration is handled by other Dendrites and the
        Cortex only wakes for terminal events.

        The Pathway auto-closes on the first FINAL or ERROR Signal;
        :meth:`stop` closes any still-open Pathways.
        """
        self._require_orchestrator("dispatch")
        if neuron is None and not capabilities:
            raise ValueError(
                "dispatch requires either neuron= (addressed) or "
                "capabilities=[...] (capability-routed)"
            )
        tid = trace_id or new_trace_id()

        # Ensure we'll observe Signals on this trace before emitting
        # anything. Subscriptions are deduplicated by ``_ensure_inbound_sub``
        # so this is cheap on repeat calls.
        await self._ensure_pathway_subs()

        # Register the Pathway BEFORE emitting so a fast-path response
        # (sub-millisecond round trip on MemorySynapse) finds it.
        pathway = Pathway(
            trace_id=tid,
            role="originator",
            on_close=self._on_pathway_close,
            scope=scope,
        )
        self._pathways[tid] = pathway

        sig = task_signal(
            trace_id=tid, parent_id=parent_id, neuron=neuron,
            input=input, context_ref=context_ref,
            capabilities=capabilities, meta=meta,
        )
        try:
            await self._publish_task(sig)
        except Exception:
            # Roll back if publish failed - the Pathway can never
            # receive a reply for a TASK that didn't make it on the bus.
            self._pathways.pop(tid, None)
            await pathway.close()
            raise

        return pathway

    async def dispatch_and_wait(
        self,
        *,
        neuron: str | None = None,
        input: dict[str, Any],
        timeout_s: float | None = 30.0,
        trace_id: str | None = None,
        parent_id: str | None = None,
        context_ref: str | None = None,
        capabilities: list[str] | None = None,
        meta: dict[str, Any] | None = None,
        scope: str = "all",
    ) -> Signal:
        """Sync-shape sugar: dispatch, block until first terminal Signal,
        close the Pathway, return the Signal.

        Equivalent to::

            async with await orch.dispatch(...) as pw:
                return await pw.wait(timeout_s=timeout_s)

        Works for both addressed (``neuron=``) and capability-routed
        (``capabilities=[...]``) dispatch, and in centralized or
        decentralized topologies. Use ``scope="terminal"`` to wait only
        for FINAL / ERROR / CLARIFICATION.

        Raises :class:`asyncio.TimeoutError` if ``timeout_s`` elapses
        before a terminal Signal arrives, and
        :class:`cosmonapse.pathway.PathwayClosedError` if the Pathway
        is closed (e.g. by Dendrite shutdown) before any matching
        Signal arrives.
        """
        pathway = await self.dispatch(
            neuron=neuron, input=input,
            trace_id=trace_id, parent_id=parent_id,
            context_ref=context_ref, capabilities=capabilities,
            meta=meta, scope=scope,
        )
        async with pathway as pw:
            return await pw.wait(timeout_s=timeout_s)

    async def dispatch_and_subscribe(
        self,
        *,
        neuron: str | None = None,
        input: dict[str, Any],
        trace_id: str | None = None,
        parent_id: str | None = None,
        context_ref: str | None = None,
        capabilities: list[str] | None = None,
        meta: dict[str, Any] | None = None,
        scope: str = "all",
    ) -> Pathway:
        """Async-shape sugar: dispatch, return the Pathway immediately.

        The caller is expected to attach ``@pw.on(...)`` callbacks (or
        iterate, or hold a reference) and let signals stream in over
        time. The Pathway still auto-closes on FINAL / ERROR  -  pass
        ``scope="terminal"`` if you only care about terminal events,
        or use :meth:`dispatch` directly for custom lifecycle.

        This is the counterpart to :meth:`dispatch_and_wait`: same
        primitive underneath, different ergonomics for the consumer.
        ``wait`` is request/reply-shaped; ``subscribe`` is event-shaped.

        Works in both centralized and decentralized topologies. In
        decentralized mode set ``scope="terminal"`` so the orchestrator
        wakes only when a workflow concludes or needs a human.
        """
        return await self.dispatch(
            neuron=neuron, input=input,
            trace_id=trace_id, parent_id=parent_id,
            context_ref=context_ref, capabilities=capabilities,
            meta=meta, scope=scope,
        )

    # -- Competitive bidding: TASK_OFFER / BID / TASK_AWARDED -------------

    async def dispatch_offer(
        self,
        *,
        input: dict[str, Any],
        capabilities: list[str] | None = None,
        deadline_ms: int = 250,
        select: str = "first_bid",
        trace_id: str | None = None,
        parent_id: str | None = None,
        context_ref: str | None = None,
        meta: dict[str, Any] | None = None,
        scope: str = "all",
    ) -> Pathway:
        """Broadcast a TASK_OFFER, collect BIDs, award the winner, and
        return a Pathway scoped to the resulting workflow.

        This is the atomic-claim variant of capability-routed dispatch:
        instead of trusting at-most-once delivery from the broker, the
        producer asks "who wants this?", listens for ``deadline_ms`` of
        BIDs, picks one per the ``select`` strategy, and emits
        TASK_AWARDED naming the winning Axon. All other bidders see
        TASK_DECLINED (informational) so they release any tentative
        reservations.

        Selection strategies:

        * ``"first_bid"``  -  first bidder wins (latency-minimising).
        * ``"lowest_cost"``  -  bidder with the smallest ``cost`` wins.
        * ``"highest_confidence"``  -  bidder with the largest
          ``confidence`` wins.

        Raises ``TimeoutError`` if no BID arrives within ``deadline_ms``.
        Only orchestrator-role Dendrites may call this.
        """
        self._require_orchestrator("dispatch_offer")
        if select not in ("first_bid", "lowest_cost", "highest_confidence"):
            raise ValueError(
                f"select must be one of 'first_bid' / 'lowest_cost' / "
                f"'highest_confidence', got {select!r}"
            )

        tid = trace_id or new_trace_id()
        await self._ensure_pathway_subs()
        # Also ensure we'll see BIDs on this trace.
        await self._ensure_inbound_sub(SignalType.BID)

        # Open the Pathway on the trace before emitting the offer so the
        # subsequent AGENT_OUTPUT / FINAL all land here.
        pathway = Pathway(
            trace_id=tid, role="originator",
            on_close=self._on_pathway_close, scope=scope,
        )
        self._pathways[tid] = pathway

        offer = task_offer_signal(
            trace_id=tid, parent_id=parent_id,
            input=input, capabilities=capabilities,
            deadline_ms=deadline_ms, meta=meta,
        )

        # Collect bids via a temporary Pathway-scoped handler.
        bids: list[Signal] = []
        bid_evt = asyncio.Event()

        @pathway.on(SignalType.BID)
        async def _collect(sig: Signal) -> None:
            bids.append(sig)
            if select == "first_bid":
                bid_evt.set()

        try:
            await self.emit(offer)
        except Exception:
            self._pathways.pop(tid, None)
            await pathway.close()
            raise

        # Wait out the bidding window. For first_bid we short-circuit
        # as soon as one arrives; for the others we drain the deadline.
        timeout_s = deadline_ms / 1000.0
        if select == "first_bid":
            try:
                await asyncio.wait_for(bid_evt.wait(), timeout=timeout_s)
            except asyncio.TimeoutError:
                await pathway.close()
                raise TimeoutError(
                    f"dispatch_offer: no BID arrived within {deadline_ms}ms"
                )
        else:
            await asyncio.sleep(timeout_s)

        if not bids:
            await pathway.close()
            raise TimeoutError(
                f"dispatch_offer: no BID arrived within {deadline_ms}ms"
            )

        # Pick the winner per strategy.
        if select == "first_bid":
            winner = bids[0]
        elif select == "lowest_cost":
            winner = min(
                bids, key=lambda b: b.payload.get("cost", float("inf")),
            )
        else:  # highest_confidence
            winner = max(
                bids, key=lambda b: b.payload.get("confidence", float("-inf")),
            )

        # Tell losers (informational; they can release any reservation).
        for b in bids:
            if b.id == winner.id:
                continue
            try:
                await self.emit(task_declined_signal(
                    trace_id=tid, parent_id=b.id,
                    neuron=b.neuron, reason="not selected",
                ))
            except Exception as exc:
                logger.warning(
                    "dispatch_offer: TASK_DECLINED emit failed for %s: %s",
                    b.neuron, exc,
                )

        # Award. The winning Axon's Dendrite will handle it via
        # _on_task_awarded -> Axon.handle_task.
        awarded = task_awarded_signal(
            trace_id=tid, parent_id=winner.id,
            neuron=winner.neuron,  # type: ignore[arg-type]
            input=input,
            winning_bid={
                k: winner.payload.get(k)
                for k in ("cost", "eta_ms", "confidence")
                if k in winner.payload
            },
            context_ref=context_ref,
        )
        try:
            await self.emit(awarded)
        except Exception:
            await pathway.close()
            raise

        return pathway

    def on_task_offer(
        self,
        fn: SignalHandler | None = None,
        *,
        capability: str | None = None,
        trace_id: str | None = None,
    ) -> Any:
        """Register a handler fired on inbound TASK_OFFER signals.

        Workers use this to evaluate offers and call :meth:`bid` to
        compete. Optional ``capability=`` filter narrows to offers
        requiring that capability; ``trace_id=`` scopes to one workflow.
        """
        return self._decorator_or_call(fn, self._on(
            SignalType.TASK_OFFER,
            neuron=None, capability=capability, trace_id=trace_id,
        ))

    async def bid(
        self,
        offer: Signal,
        *,
        neuron: str,
        cost: float,
        eta_ms: int | None = None,
        confidence: float | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Signal:
        """Emit a BID in response to a TASK_OFFER.

        Called from inside an :meth:`on_task_offer` handler. The
        ``neuron`` argument names the local Axon that would handle the
        work; the producer's :meth:`dispatch_offer` collects the BIDs
        and addresses the eventual TASK_AWARDED to the winner's
        ``neuron``.

        BID bypasses the role guard so worker-role Dendrites can
        participate in capability routing  -  bidding is how a worker
        announces "I can take this work", not orchestration.
        """
        if offer.type is not SignalType.TASK_OFFER:
            raise DendriteProtocolError(
                f"bid() expects a TASK_OFFER signal, got {offer.type.value!r}"
            )
        sig = bid_signal(
            trace_id=offer.trace_id, parent_id=offer.id,
            neuron=neuron, cost=cost, eta_ms=eta_ms,
            confidence=confidence, meta=meta,
        )
        # _publish bypasses the orchestrator guard in emit()  -  a worker
        # bidding is announcing capability, not dispatching work.
        await self._publish(sig)
        return sig

    async def observe_pathway(self, trace_id: str) -> Pathway:
        """Open a Pathway in *observer* role for a trace this Dendrite did
        not originate.

        Use this to subscribe to a workflow that another peer started.
        Signals matching ``trace_id`` that reach this Dendrite are
        delivered into the returned Pathway; no TASK is emitted.

        Raises ``ValueError`` if a Pathway already exists for this
        ``trace_id`` on this Dendrite.
        """
        if trace_id in self._pathways:
            raise ValueError(
                f"Dendrite already has a Pathway open for trace {trace_id!r}"
            )
        await self._ensure_pathway_subs()
        pathway = Pathway(
            trace_id=trace_id,
            role="observer",
            on_close=self._on_pathway_close,
        )
        self._pathways[trace_id] = pathway
        return pathway

    async def _ensure_pathway_subs(self) -> None:
        """Subscribe to every PATHWAY_TYPES Signal so trace-matching can
        route inbound Signals to open Pathways. Subscriptions are
        deduplicated, so this is idempotent."""
        for st in PATHWAY_TYPES:
            await self._ensure_inbound_sub(st)

    async def _on_pathway_close(self, pathway: Pathway) -> None:
        """Called by a Pathway when it closes - evict from the registry."""
        self._pathways.pop(pathway.trace_id, None)

    async def emit_final(self, *, trace_id: str, parent_id: str, result: Any, meta: dict[str, Any] | None = None) -> Signal:
        sig = final_signal(trace_id=trace_id, parent_id=parent_id,
                           neuron=self.dendrite_id, result=result, meta=meta)
        await self.emit(sig)
        return sig

    async def emit_error(self, *, trace_id: str, parent_id: str, code: str, message: str,
                         recoverable: bool = False, meta: dict[str, Any] | None = None) -> Signal:
        sig = error_signal(trace_id=trace_id, parent_id=parent_id,
                           neuron=self.dendrite_id, code=code, message=message,
                           recoverable=recoverable, meta=meta)
        await self.emit(sig)
        return sig

    # -- Cognition emit helpers ------------------------------------------

    async def emit_plan(self, *, trace_id: str, parent_id: str, steps: list[Any],
                        rationale: str | None = None, neuron: str | None = None, meta: dict[str, Any] | None = None) -> Signal:
        sig = plan_signal(
            trace_id=trace_id, parent_id=parent_id,
            neuron=neuron or self.dendrite_id,
            steps=steps, rationale=rationale, meta=meta,
        )
        await self.emit(sig)
        return sig

    async def emit_thought_delta(self, *, trace_id: str, parent_id: str, delta: str,
                                 seq: int | None = None, neuron: str | None = None, meta: dict[str, Any] | None = None) -> Signal:
        sig = thought_delta_signal(
            trace_id=trace_id, parent_id=parent_id,
            neuron=neuron or self.dendrite_id,
            delta=delta, seq=seq, meta=meta,
        )
        await self.emit(sig)
        return sig

    async def emit_tool_call(self, *, trace_id: str, parent_id: str, tool: str, args: dict[str, Any],
                             call_id: str | None = None, neuron: str | None = None, meta: dict[str, Any] | None = None) -> Signal:
        sig = tool_call_signal(
            trace_id=trace_id, parent_id=parent_id,
            neuron=neuron or self.dendrite_id,
            tool=tool, args=args, call_id=call_id, meta=meta,
        )
        await self.emit(sig)
        return sig

    async def emit_tool_result(self, *, trace_id: str, parent_id: str, tool: str,
                               result: Any = None, error: Any = None, call_id: str | None = None,
                               neuron: str | None = None, meta: dict[str, Any] | None = None) -> Signal:
        sig = tool_result_signal(
            trace_id=trace_id, parent_id=parent_id,
            neuron=neuron or self.dendrite_id,
            tool=tool, result=result, error=error, call_id=call_id, meta=meta,
        )
        await self.emit(sig)
        return sig

    async def emit_memory_append(self, *, trace_id: str, parent_id: str, key: str, value: Any,
                                 neuron: str | None = None, meta: dict[str, Any] | None = None) -> Signal:
        sig = memory_append_signal(
            trace_id=trace_id, parent_id=parent_id,
            neuron=neuron or self.dendrite_id,
            key=key, value=value, meta=meta,
        )
        await self.emit(sig)
        return sig

    async def emit_critique(self, *, trace_id: str, parent_id: str, target_event_id: str,
                            issues: list[Any], verdict: str, neuron: str | None = None, meta: dict[str, Any] | None = None) -> Signal:
        sig = critique_signal(
            trace_id=trace_id, parent_id=parent_id,
            neuron=neuron or self.dendrite_id,
            target_event_id=target_event_id,
            issues=issues, verdict=verdict, meta=meta,
        )
        await self.emit(sig)
        return sig

    async def emit_escalation(self, *, trace_id: str, parent_id: str, reason: str,
                              target: str | None = None, context: dict[str, Any] | None = None,
                              neuron: str | None = None, meta: dict[str, Any] | None = None) -> Signal:
        sig = escalation_signal(
            trace_id=trace_id, parent_id=parent_id,
            neuron=neuron or self.dendrite_id,
            reason=reason, target=target, context=context, meta=meta,
        )
        await self.emit(sig)
        return sig

    async def emit_consensus(self, *, trace_id: str, parent_id: str, members: list[str], verdict: str,
                             votes: dict[str, Any] | None = None, neuron: str | None = None, meta: dict[str, Any] | None = None) -> Signal:
        sig = consensus_signal(
            trace_id=trace_id, parent_id=parent_id,
            neuron=neuron or self.dendrite_id,
            members=members, verdict=verdict, votes=votes, meta=meta,
        )
        await self.emit(sig)
        return sig

    async def emit_context_sync(self, *, trace_id: str, parent_id: str, snapshot: dict[str, Any],
                                version: str | None = None, neuron: str | None = None, meta: dict[str, Any] | None = None) -> Signal:
        sig = context_sync_signal(
            trace_id=trace_id, parent_id=parent_id,
            neuron=neuron or self.dendrite_id,
            snapshot=snapshot, version=version, meta=meta,
        )
        await self.emit(sig)
        return sig

    # -- Close-the-loop helpers ------------------------------------------
    # A CLARIFICATION or ESCALATION is half a conversation - the
    # orchestrator's natural reply is a follow-up TASK that keeps the
    # original lineage (parent_id -> the prompting signal, trace_id
    # carried over) so observers can follow the workflow.

    async def respond_to_clarification(
        self,
        signal: Signal,
        *,
        answer: Any,
        extra: dict[str, Any] | None = None,
        neuron: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Signal:
        """Reply to a CLARIFICATION by re-dispatching a TASK with the answer.

        The new TASK is addressed by default to the Neuron that asked
        the question (``signal.neuron``), with ``parent_id`` = the
        clarification's id and the original ``trace_id`` carried over.

        New TASK input shape::

            {"clarification": {
                "question": <from signal.payload>,
                "answer":   <answer>,
                **(extra or {}),
             }}

        Pass ``neuron=`` to route the follow-up elsewhere.

        Raises ``DendriteProtocolError`` if ``signal`` isn't a
        CLARIFICATION or no target can be resolved.
        """
        if signal.type is not SignalType.CLARIFICATION:
            raise DendriteProtocolError(
                f"respond_to_clarification expects a CLARIFICATION signal, "
                f"got {signal.type.value!r}"
            )
        target = neuron or signal.neuron
        if not target:
            raise DendriteProtocolError(
                "respond_to_clarification: signal has no neuron and no "
                "neuron= override - nowhere to dispatch the follow-up TASK"
            )
        payload: dict[str, Any] = {
            "question": signal.payload.get("question"),
            "answer": answer,
        }
        if extra:
            payload.update(extra)
        return await self.dispatch_task(
            neuron=target,
            input={"clarification": payload},
            trace_id=signal.trace_id,
            parent_id=signal.id,
            meta=meta,
        )

    async def respond_to_escalation(
        self,
        signal: Signal,
        *,
        neuron: str | None = None,
        input: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Signal:
        """Reply to an ESCALATION by dispatching a TASK to the escalation target.

        Default target: ``signal.payload['target']`` (the target the
        escalating Neuron requested). Pass ``neuron=`` to override.

        Default input::

            {"escalation": {
                "reason":  signal.payload['reason'],
                "context": signal.payload.get('context'),
                "from":    signal.neuron,
             }}

        Pass ``input=`` to override.

        Raises ``DendriteProtocolError`` if ``signal`` isn't an ESCALATION
        or no target can be resolved.
        """
        if signal.type is not SignalType.ESCALATION:
            raise DendriteProtocolError(
                f"respond_to_escalation expects an ESCALATION signal, "
                f"got {signal.type.value!r}"
            )
        target = neuron or signal.payload.get("target")
        if not target:
            raise DendriteProtocolError(
                "respond_to_escalation: signal has no payload.target and "
                "no neuron= override - nowhere to dispatch the follow-up TASK"
            )
        if input is None:
            input = {"escalation": {
                "reason": signal.payload.get("reason"),
                "context": signal.payload.get("context"),
                "from": signal.neuron,
            }}
        return await self.dispatch_task(
            neuron=target,
            input=input,
            trace_id=signal.trace_id,
            parent_id=signal.id,
            meta=meta,
        )

    async def respond_to_permission(
        self,
        signal: Signal,
        *,
        granted: bool,
        reason: str | None = None,
        ttl_ms: int | None = None,
        extra: dict[str, Any] | None = None,
        neuron: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Signal:
        """Reply to a PERMISSION by re-dispatching a TASK carrying the verdict.

        The mirror of :meth:`respond_to_clarification`. The follow-up TASK is
        addressed by default to the Neuron that asked (``signal.neuron``), with
        ``parent_id`` = the PERMISSION's id and the original ``trace_id``
        carried over, so the Neuron resumes on the same thread and can imprint
        the decision into an Engram (or recall it next time).

        New TASK input shape::

            {"permission": {
                "action":  <from signal.payload>,
                "granted": <granted>,
                "reason":  <reason>,
                "ttl_ms":  <ttl_ms>,
                **(extra or {}),
             }}
        """
        if signal.type is not SignalType.PERMISSION:
            raise DendriteProtocolError(
                f"respond_to_permission expects a PERMISSION signal, "
                f"got {signal.type.value!r}"
            )
        target = neuron or signal.neuron
        if not target:
            raise DendriteProtocolError(
                "respond_to_permission: signal has no neuron and no neuron= "
                "override - nowhere to dispatch the follow-up TASK"
            )
        payload: dict[str, Any] = {
            "action": signal.payload.get("action"),
            "granted": bool(granted),
        }
        if reason is not None:
            payload["reason"] = reason
        if ttl_ms is not None:
            payload["ttl_ms"] = ttl_ms
        if extra:
            payload.update(extra)
        return await self.dispatch_task(
            neuron=target,
            input={"permission": payload},
            trace_id=signal.trace_id,
            parent_id=signal.id,
            meta=meta,
        )

    # -- Cognition decision signals (discrete, decentralised option) ------
    # Thin, stateless emit helpers for the new response signal types - no
    # correlation client. Use these when you want the decision to travel as a
    # discrete PERMISSION_DECISION / CLARIFICATION_ANSWER signal (e.g. for a
    # peer/observer to imprint into an Engram) rather than as a re-dispatched
    # TASK. Published via the private ``_publish`` path so any Dendrite -
    # including a worker-role peer - can answer. Correlation, if needed, is the
    # developer's choice (parent_id == the request's id).

    async def grant_permission(
        self,
        request: Signal,
        *,
        reason: str | None = None,
        ttl_ms: int | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Signal:
        """Approve a PERMISSION request. ``ttl_ms`` optionally advertises how
        long the grant is valid so the requester can cache it in an Engram."""
        return await self._decide_permission(
            request, granted=True, reason=reason, ttl_ms=ttl_ms, meta=meta,
        )

    async def deny_permission(
        self,
        request: Signal,
        *,
        reason: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Signal:
        """Reject a PERMISSION request."""
        return await self._decide_permission(
            request, granted=False, reason=reason, ttl_ms=None, meta=meta,
        )

    async def _decide_permission(
        self,
        request: Signal,
        *,
        granted: bool,
        reason: str | None,
        ttl_ms: int | None,
        meta: dict[str, Any] | None,
    ) -> Signal:
        if request.type is not SignalType.PERMISSION:
            raise DendriteProtocolError(
                f"grant/deny_permission expects a PERMISSION signal, got "
                f"{request.type.value!r}"
            )
        sig = permission_decision_signal(
            trace_id=request.trace_id,
            parent_id=request.id,
            granted=granted,
            neuron=self.dendrite_id,
            reason=reason,
            ttl_ms=ttl_ms,
            meta=meta,
        )
        await self._publish(sig)
        return sig

    async def answer_clarification(
        self,
        request: Signal,
        *,
        answer: Any,
        meta: dict[str, Any] | None = None,
    ) -> Signal:
        """Answer a *blocking* CLARIFICATION (the Neuron called ``ask(...)``
        and is awaiting). This is distinct from
        :meth:`respond_to_clarification`, which closes the legacy
        return-marker flow by re-dispatching a TASK."""
        if request.type is not SignalType.CLARIFICATION:
            raise DendriteProtocolError(
                f"answer_clarification expects a CLARIFICATION signal, got "
                f"{request.type.value!r}"
            )
        sig = clarification_answer_signal(
            trace_id=request.trace_id,
            parent_id=request.id,
            answer=answer,
            neuron=self.dendrite_id,
            meta=meta,
        )
        await self._publish(sig)
        return sig

    async def emit(self, signal: Signal) -> None:
        """Emit a synapse-side Signal (orchestration).

        Funnel for every public emit_* helper (emit_final / emit_error /
        emit_plan / emit_critique / ...). Subject to two guards:

        * Role guard - only orchestrator-role Dendrites may emit
          orchestration Signals. Worker Axons still publish AGENT_OUTPUT
          / CLARIFICATION / ERROR via the private ``_publish`` path so
          ``handle_task`` replies are unaffected. ``bid()`` also uses
          ``_publish`` so workers can compete in capability routing.
        * Type guard - only SYNAPSE_TYPES are accepted; AXON_TYPES
          (REGISTER / HEARTBEAT / DEREGISTER / AGENT_OUTPUT / ...) must
          go through the internal management paths, not this method.
        """
        self._require_orchestrator(f"emit({signal.type.value})")
        if signal.type not in SYNAPSE_TYPES:
            raise DendriteProtocolError(
                f"Dendrite refuses to emit {signal.type.value!r}: "
                f"only synapse-side types (SYNAPSE_TYPES) may be emitted "
                f"this way. {signal.type.value} is an Axon-owned type."
            )
        await self._publish(signal)

    async def _publish(self, signal: Signal) -> None:
        await self._synapse.publish(self._subject(signal.type), signal)

    async def subscribe(
        self,
        signal_type: SignalType,
        handler: MessageHandler,
        *,
        queue_group: str | None = None,
    ) -> Subscription:
        return await self._synapse.subscribe(
            self._subject(signal_type),
            handler,
            queue_group=queue_group,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _subject(self, signal_type: SignalType) -> str:
        return f"cosmonapse.{self._namespace}.{signal_type.value}"

    def _routed_subject(self) -> str:
        """Subject for capability-routed TASKs.

        Distinct from ``_subject(SignalType.TASK)`` so the addressed and
        routed flows can use different delivery semantics: addressed is
        broadcast (every Dendrite filters), routed uses queue groups
        (one Dendrite per group consumes).
        """
        return f"cosmonapse.{self._namespace}.{SignalType.TASK.value}.routed"

    async def _ensure_inbound_sub(self, signal_type: SignalType) -> None:
        if signal_type in self._inbound_subs:
            return
        sub = await self.subscribe(signal_type, self._dispatch_inbound)
        self._inbound_subs[signal_type] = sub

    async def _on_task(self, task: Signal) -> None:
        """Route an inbound TASK to a local Axon, if any.

        Addressed (``task.neuron`` set): look up that Axon by neuron_id;
        if not hosted here, drop silently. Capability-routed (no neuron,
        capabilities in payload): pick the first local Axon whose
        capabilities superset the request and route there.

        Capability-routed TASKs may be processed by more than one
        Dendrite if multiple have a covering Axon (at-least-once across
        the matching set). Use TASK_OFFER / BID for atomic claim.
        """
        target = task.neuron
        axon: Axon | None = None

        if target:
            axon = self._axons.get(target)
            if axon is None:
                return
        else:
            requested = task.payload.get("capabilities") or []
            if not requested:
                return
            req_set = set(requested)
            for candidate in self._axons.values():
                if req_set.issubset(set(candidate.capabilities)):
                    axon = candidate
                    break
            if axon is None:
                return
            target = axon.neuron_id

        try:
            reply = await axon.handle_task(task)
        except Exception as exc:
            logger.exception("Dendrite: Axon %s raised unexpectedly", target)
            reply = error_signal(
                trace_id=task.trace_id, parent_id=task.id,
                neuron=target, code="AXON_EXCEPTION",
                message=str(exc), recoverable=False,
            )
        await self._publish(reply)

    async def _handle_discover(self, signal: Signal) -> None:
        if self._discover_handlers:
            results = await asyncio.gather(
                *(h(signal) for h in self._discover_handlers),
                return_exceptions=True,
            )
            for r in results:
                if isinstance(r, Exception):
                    logger.exception(
                        "Dendrite on_discover handler raised: %s", r, exc_info=r,
                    )
            return
        await self.respond_to_discover(signal)

    async def respond_to_discover(self, signal: Signal) -> None:
        if not self._axons:
            return
        payload = signal.payload or {}
        target = payload.get("neuron")
        caps_filter = payload.get("capabilities")
        await asyncio.sleep(random.uniform(0, 0.1))
        caps_set: set[str] | None = (
            set(caps_filter) if caps_filter else None
        )
        for axon in self._axons.values():
            if target and axon.neuron_id != target:
                continue
            if caps_set and not caps_set.issubset(set(axon.capabilities)):
                continue
            try:
                await self._emit_register(axon)
            except Exception as exc:
                logger.warning(
                    "Dendrite: DISCOVER response (REGISTER) failed for %s: %s",
                    axon.neuron_id, exc,
                )

    async def _emit_register(self, axon: Axon) -> None:
        await self._publish(register_signal(
            neuron=axon.neuron_id,
            capabilities=axon.capabilities,
            version=axon.version,
        ))

    async def _emit_deregister(self, axon: Axon, *, reason: str | None) -> None:
        await self._publish(deregister_signal(neuron=axon.neuron_id, reason=reason))

    async def _emit_discover(
        self,
        *,
        neuron: str | None = None,
        capabilities: list[str] | None = None,
    ) -> None:
        await self._publish(discover_signal(
            neuron=neuron, capabilities=capabilities,
        ))

    async def _mirror_to_store(self, axon: Axon, *, status: str) -> None:
        if self._registry_store is None:
            return
        try:
            await self._registry_store.upsert(NeuronRecord(
                neuron_id=axon.neuron_id,
                capabilities=list(axon.capabilities),
                version=axon.version,
                status=status,
                last_heartbeat=datetime.now(timezone.utc),
            ))
        except Exception as exc:
            logger.warning("Dendrite: failed to mirror %s into store: %s",
                           axon.neuron_id, exc)

    async def _heartbeat_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._heartbeat_s)
            except asyncio.CancelledError:
                return
            if not self._running:
                return
            now = datetime.now(timezone.utc)
            for axon in self._axons.values():
                try:
                    if self._reregister_on_heartbeat:
                        await self._emit_register(axon)
                    await self._synapse.publish(
                        self._subject(SignalType.HEARTBEAT),
                        heartbeat_signal(neuron=axon.neuron_id),
                    )
                except Exception as exc:
                    logger.warning("Heartbeat publish failed for %s: %s",
                                   axon.neuron_id, exc)
                if self._registry_store is not None:
                    try:
                        await self._registry_store.touch_heartbeat(axon.neuron_id, now)
                    except Exception:
                        pass
                try:
                    await axon._on_heartbeat_tick()
                except Exception as exc:
                    logger.warning("Axon heartbeat-tick hook failed: %s", exc)
            await self._fire_refresh(RefreshEvent(reason="heartbeat"))

    async def _dispatch_inbound(self, signal: Signal) -> None:
        if signal.type == SignalType.DISCOVER:
            await self._handle_discover(signal)
            return

        # Engram I/O: route RECALL/IMPRINT to a hosted Engram if any
        # matches; deliver RECALLED/IMPRINTED to the local EngramClient
        # so awaiting Futures resolve.
        if signal.type is SignalType.RECALL:
            await self._on_recall(signal)
            return
        if signal.type is SignalType.IMPRINT:
            await self._on_imprint(signal)
            return
        if signal.type in (SignalType.RECALLED, SignalType.IMPRINTED):
            try:
                await self._engram_client._deliver(signal)
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "Dendrite: EngramClient delivery failed for %s: %s",
                    signal.type.value, exc,
                )
            # Continue to user-registered handlers below (e.g. observers).

        # Trace terminal events cancel any in-flight engram I/O on the
        # same trace so awaiters in Neurons / orchestrators wake up
        # instead of hanging on a deadline.
        if signal.type in (SignalType.FINAL, SignalType.ERROR) and signal.trace_id:
            try:
                self._engram_client.cancel_trace(signal.trace_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Dendrite: cancel_trace failed for %s: %s",
                    signal.trace_id, exc,
                )

        # TASK_AWARDED targeting one of our Axons: synthesise a TASK
        # and route through the existing Axon handler.
        if signal.type is SignalType.TASK_AWARDED:
            target = signal.neuron
            if target and target in self._axons:
                synthetic = task_signal(
                    trace_id=signal.trace_id, parent_id=signal.id,
                    neuron=target,
                    input=signal.payload.get("input", {}),
                    context_ref=signal.payload.get("context_ref"),
                    meta=signal.meta,
                )
                await self._on_task(synthetic)

        if signal.trace_id and signal.type in PATHWAY_TYPES:
            pathway = self._pathways.get(signal.trace_id)
            if pathway is not None:
                try:
                    await pathway._deliver(signal)
                except Exception as exc:
                    logger.exception(
                        "Dendrite: Pathway delivery failed for %s on trace %s: %s",
                        signal.type.value, signal.trace_id, exc,
                    )

        if signal.type in AXON_TYPES and self._registry_store is not None:
            try:
                await self._update_registry(signal)
            except Exception as exc:
                logger.exception(
                    "Dendrite: registry update failed for %s: %s",
                    signal.type.value, exc,
                )
        handlers = self._handlers.get(signal.type, [])
        if handlers:
            results = await asyncio.gather(
                *(h(signal) for h in handlers),
                return_exceptions=True,
            )
            for r in results:
                if isinstance(r, Exception):
                    logger.exception(
                        "Dendrite handler for %s raised: %s",
                        signal.type.value, r, exc_info=r,
                    )

    async def _update_registry(self, signal: Signal) -> None:
        if self._registry_store is None:
            return
        neuron_id = signal.neuron
        if not neuron_id:
            return
        reason: str | None = None
        if signal.type == SignalType.REGISTER:
            await self._registry_store.upsert(NeuronRecord(
                neuron_id=neuron_id,
                capabilities=list(signal.payload.get("capabilities", [])),
                version=signal.payload.get("version"),
                status="registered",
                last_heartbeat=signal.ts,
            ))
            reason = "register"
        elif signal.type == SignalType.DEREGISTER:
            await self._registry_store.mark_deregistered(neuron_id)
            reason = "deregister"
        elif signal.type == SignalType.HEARTBEAT:
            await self._registry_store.touch_heartbeat(
                neuron_id, signal.ts,
                status=signal.payload.get("status"),
            )
            reason = "heartbeat"
        if reason is not None:
            await self._fire_refresh(RefreshEvent(
                reason=reason, neuron_id=neuron_id,
            ))


    # ------------------------------------------------------------------
    # Engram: hosted-side handlers
    # ------------------------------------------------------------------

    def _resolve_engram_targets(self, signal: Signal) -> list[Engram]:
        """Pick the hosted Engrams that should respond to a RECALL/IMPRINT.

        engram_id wins over engram_kind. If neither matches a hosted
        Engram, returns []. If engram_kind matches multiple hosted
        Engrams, every match is returned  -  recall_mode handles the
        winner-selection on the caller side.
        """
        eid = signal.payload.get("engram_id")
        if eid:
            ent = self._engrams.get(eid)
            return [ent] if ent is not None else []
        ekind = signal.payload.get("engram_kind")
        if ekind:
            return [
                self._engrams[i]
                for i in self._engram_kind_index.get(ekind, [])
                if i in self._engrams
            ]
        return []

    async def _on_recall(self, signal: Signal) -> None:
        targets = self._resolve_engram_targets(signal)
        if not targets:
            return
        query = signal.payload.get("query") or {}
        filters = signal.payload.get("filters")
        context_ref = signal.payload.get("context_ref")
        deadline_ms = signal.payload.get("deadline_ms")
        min_confidence = signal.payload.get("min_confidence")
        for engram in targets:
            try:
                if not await engram.can_serve(query):
                    continue
                hits = await engram.recall(
                    query,
                    filters=filters,
                    context_ref=context_ref,
                    deadline_ms=deadline_ms,
                    min_confidence=min_confidence,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "Dendrite: Engram %s.recall raised: %s",
                    engram.engram_id, exc,
                )
                continue
            reply = recalled_signal(
                trace_id=signal.trace_id,
                parent_id=signal.id,
                engram_id=engram.engram_id,
                hits=[
                    {"id": h.id, "entry": h.entry, "score": h.score}
                    for h in hits
                ],
                neuron=self.dendrite_id,
            )
            try:
                await self._publish(reply)
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "Dendrite: Engram %s RECALLED publish failed: %s",
                    engram.engram_id, exc,
                )

    async def _on_imprint(self, signal: Signal) -> None:
        targets = self._resolve_engram_targets(signal)
        if not targets:
            return
        op = signal.payload.get("op", "")
        entry = signal.payload.get("entry") or {}
        merge_key = signal.payload.get("merge_key")
        for engram in targets:
            try:
                receipt = await engram.imprint(
                    op, entry, merge_key=merge_key, imprint_id=signal.id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "Dendrite: Engram %s.imprint raised: %s",
                    engram.engram_id, exc,
                )
                receipt = None
                err_msg = f"engram_exception: {exc}"
                reply = imprinted_signal(
                    trace_id=signal.trace_id,
                    parent_id=signal.id,
                    engram_id=engram.engram_id,
                    op=op,
                    error=err_msg,
                    neuron=self.dendrite_id,
                )
            else:
                assert receipt is not None
                reply = imprinted_signal(
                    trace_id=signal.trace_id,
                    parent_id=signal.id,
                    engram_id=receipt.engram_id or engram.engram_id,
                    op=receipt.op,
                    id=receipt.id,
                    version=receipt.version,
                    took_ms=receipt.took_ms,
                    error=receipt.error,
                    neuron=self.dendrite_id,
                )
            try:
                await self._publish(reply)
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "Dendrite: Engram %s IMPRINTED publish failed: %s",
                    engram.engram_id, exc,
                )

    # ------------------------------------------------------------------
    # Engram: caller-side helpers (used by orchestrating Dendrites and
    # Cortex code; Axon helpers go through _engram_client directly).
    # ------------------------------------------------------------------

    @property
    def engram_client(self) -> EngramClient:
        """Caller-side correlation table. Surfaced for the Axon to call
        directly without going through dendrite.recall/imprint."""
        return self._engram_client

    async def recall(
        self,
        *,
        engram_id: str | None = None,
        engram_kind: str | None = None,
        query: dict[str, Any],
        filters: dict[str, Any] | None = None,
        context_ref: str | None = None,
        deadline_ms: int | None = None,
        recall_mode: str = "first",
        min_confidence: float | None = None,
        trace_id: str | None = None,
        parent_id: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Any:
        """Emit RECALL and await RECALLED.

        When ``trace_id`` is omitted a new trace is minted  -  use this for
        pre-task hydration. Inside a TASK context (e.g. the Cortex
        servicing an AGENT_OUTPUT), pass ``trace_id`` and ``parent_id``
        so the recall is attributed to the containing workflow per
        ENGRAM_DESIGN.md §5.4.
        """
        tid = trace_id or new_trace_id()
        pid = parent_id
        if pid is None:
            # Synthesise a root parent_id so the envelope validates.
            # Callers inside a TASK should supply parent_id explicitly.
            from cosmonapse.envelope import new_event_id
            pid = new_event_id()
        return await self._engram_client.recall(
            engram_id=engram_id,
            engram_kind=engram_kind,
            query=query,
            filters=filters,
            context_ref=context_ref,
            deadline_ms=deadline_ms,
            recall_mode=recall_mode,
            min_confidence=min_confidence,
            trace_id=tid,
            parent_id=pid,
            neuron=self.dendrite_id,
            meta=meta,
        )

    async def imprint(
        self,
        *,
        engram_id: str | None = None,
        engram_kind: str | None = None,
        op: str,
        entry: dict[str, Any],
        merge_key: str | None = None,
        await_ack: bool = False,
        deadline_ms: int | None = None,
        trace_id: str | None = None,
        parent_id: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Any:
        """Emit IMPRINT. Returns None unless ``await_ack=True``."""
        tid = trace_id or new_trace_id()
        pid = parent_id
        if pid is None:
            from cosmonapse.envelope import new_event_id
            pid = new_event_id()
        return await self._engram_client.imprint(
            engram_id=engram_id,
            engram_kind=engram_kind,
            op=op,
            entry=entry,
            merge_key=merge_key,
            await_ack=await_ack,
            deadline_ms=deadline_ms,
            trace_id=tid,
            parent_id=pid,
            neuron=self.dendrite_id,
            meta=meta,
        )


# Back-compat alias - Cortex is just a Dendrite.
Cortex = Dendrite
 