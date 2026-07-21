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
from cosmonapse.effector.base import Effector, ToolOutcome
from cosmonapse.effector.client import EffectorClient
from cosmonapse.engram.base import Engram
from cosmonapse.engram.client import EngramClient
from cosmonapse.envelope import (
    AXON_TYPES,
    SYNAPSE_TYPES,
    Directed,
    Signal,
    SignalType,
    ambient_trace,
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
    stop_signal,
    stopped_signal,
    task_awarded_signal,
    task_declined_signal,
    task_offer_signal,
    task_signal,
    thought_delta_signal,
    tool_call_signal,
    tool_result_signal,
)
from cosmonapse.pathway import PATHWAY_TYPES, Pathway, PathwayClosedError
from cosmonapse.retry import RetryStrategy
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
        auto_bid: bool = True,
        stale_after_s: float | None = None,
    ) -> None:
        """``auto_bid``: when True (default), a Dendrite hosting Axons
        answers TASK_OFFERs out of the box  -  if no user ``on_task_offer``
        handler is registered and a hosted Axon's capabilities cover the
        offer's requested set, it bids (cost=0.0, confidence=1.0) on that
        Axon's behalf. Registering your own ``on_task_offer`` handler
        suppresses the default bidder entirely, so custom bidding logic
        never competes with it. Set False to opt out."""
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
        self._auto_bid = auto_bid
        # Liveness: a registered Neuron whose last_heartbeat is older than
        # this is marked deregistered by the heartbeat loop's sweep, so
        # find_neurons() stops returning ghosts. Default: 3 heartbeat
        # intervals. 0/None with heartbeat_s=0 disables the sweep.
        if stale_after_s is None:
            stale_after_s = heartbeat_s * 3 if heartbeat_s > 0 else 0.0
        self._stale_after_s = stale_after_s

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
        self._pending_sub_tasks: set[asyncio.Task[None]] = set()
        # In-flight subscription attempts, deduplicated per type so two
        # concurrent _ensure_inbound_sub calls can't double-subscribe (which
        # would make every handler fire twice). Event-based (not Task-based)
        # so the first caller subscribes INLINE - no extra event-loop hop,
        # which timing-sensitive late registrations rely on.
        self._inflight_subs: dict[SignalType, asyncio.Event] = {}
        # Recently seen CLARIFICATION_ANSWER / PERMISSION_DECISION keyed by
        # parent_id, so await_decision can serve an answer that arrived
        # before it was called (an in-process synapse can deliver the whole
        # request->answer chain within the original publish). Bounded FIFO.
        self._recent_decisions: dict[str, Signal] = {}
        self._inbound_subs: dict[SignalType, Subscription] = {}
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._running = False

        # Open Pathways keyed by trace_id. Populated by dispatch() and
        # observe_pathway(); evicted on Pathway.close() via _on_pathway_close.
        self._pathways: dict[str, Pathway] = {}

        # Per-operation Pathways keyed by the issuing request's envelope id
        # (== the response's parent_id). Opened by _open_op_pathway() for
        # request/reply correlation - this is what makes EngramClient a thin
        # wrapper over a Pathway rather than its own Future table. Evicted on
        # close via _on_op_pathway_close; closed for a trace on its terminal
        # event (FINAL/ERROR) and on stop().
        self._op_pathways: dict[str, Pathway] = {}

        # In-flight neuron handler tasks keyed by trace_id, so a STOP can
        # cancel exactly the work belonging to one workflow. Populated by
        # _on_task; drained when a handler finishes or is cancelled.
        self._trace_tasks: dict[str, set[asyncio.Task[Any]]] = {}

        # Attached Engrams keyed by engram_id. Routing also indexes by
        # engram_kind so RECALL/IMPRINT with engram_kind= addresses all
        # matching hosts (typically one per kind by deployment convention).
        # The EngramClient owns the caller-side correlation table for
        # in-flight RECALL/IMPRINT awaiting RECALLED/IMPRINTED.
        self._engrams: dict[str, Engram] = {}
        self._engram_kind_index: dict[str, list[str]] = {}
        self._engram_client: EngramClient = EngramClient(self)

        # Attached Effectors keyed by effector_id, with an effector_kind
        # index so a TOOL_CALL with directed.type addresses every hosted
        # match - the action-side mirror of the Engram tables above.
        self._effectors: dict[str, Effector] = {}
        self._effector_kind_index: dict[str, list[str]] = {}
        self._effector_client: EffectorClient = EffectorClient(self)

        # Engrams learned from REGISTER signals (possibly out-of-process).
        # Keyed by directed.id (engram_id); a kind index mirrors
        # directed.type (engram_kind). These record the namespace-wide view
        # of reachable Engrams - the actual RECALL/IMPRINT delivery to an
        # out-of-process participant rides the broadcast RECALL/IMPRINT
        # subject (the hosting Dendrite serves it). In-process Engrams
        # attached via attach_engram are served directly and additionally
        # announce themselves with REGISTER on start.
        self._engram_registrations: dict[str, Directed] = {}
        self._engram_reg_kind_index: dict[str, set[str]] = {}

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

    # Signal types whose *initiation* requires orchestrator role. Only TASK
    # is gated: a non-orchestrator must not start new task work. Every other
    # synapse-side signal - cognition (PLAN / CRITIQUE / ...), replies
    # (FINAL / ERROR / CLARIFICATION_ANSWER / PERMISSION_DECISION) and memory
    # (RECALL / IMPRINT) - is role-agnostic, so interactions like recall /
    # imprint can run over the generic dispatch + Pathway path from any role.
    # dispatch_offer still guards itself at entry, so competitive-bidding
    # initiation stays orchestrator-only independent of this set.
    _ROLE_GATED_TYPES: frozenset[SignalType] = frozenset({SignalType.TASK, SignalType.STOP})

    def _require_orchestrator(self, op: str) -> None:
        """Guard for TASK initiation. Only orchestrator-role Dendrites may
        dispatch TASK signals; all other signal emission is role-agnostic."""
        if self._role != "orchestrator":
            raise DendriteProtocolError(
                f"Dendrite role={self._role!r} cannot perform {op!r}: "
                f"only role='orchestrator' Dendrites may dispatch TASK "
                f"signals. Workers host Axons and emit replies / cognition / "
                f"memory signals (AGENT_OUTPUT / FINAL / RECALL / ...) freely."
            )

    # ------------------------------------------------------------------
    # Attachment
    # ------------------------------------------------------------------

    def attach_axon(self, axon: Axon) -> None:
        """Attach an Axon to a *stopped* Dendrite.

        Raises ``RuntimeError`` if the Dendrite is running  -  a running
        Dendrite needs the async activation path (TASK subscriptions,
        queue-group refresh, REGISTER emission): use
        ``await dendrite.add_axon(axon)`` instead, which works in both
        states.
        """
        if self._running:
            raise RuntimeError(
                "attach_axon on a running Dendrite would never receive "
                "TASKs (no subscription / REGISTER is set up after "
                "start). Use `await dendrite.add_axon(axon)` instead."
            )
        self._attach_axon_record(axon)

    def _attach_axon_record(self, axon: Axon) -> None:
        if axon.neuron_id in self._axons:
            raise ValueError(
                f"Dendrite already has an Axon for neuron_id={axon.neuron_id!r}"
            )
        self._axons[axon.neuron_id] = axon
        axon.attach_to(self)

    async def add_axon(self, axon: Axon) -> None:
        """Attach an Axon; if the Dendrite is running, activate it live.

        Live activation mirrors what ``start()`` does for axons attached
        up front: ensure the addressed + routed TASK subscriptions exist
        (re-keying the routed queue group for the new aggregate cap
        profile), subscribe TASK_AWARDED / DISCOVER, mirror to the
        registry store, emit REGISTER, and fire the Axon's on_connect
        hooks.
        """
        self._attach_axon_record(axon)
        if not self._running:
            return
        if self._task_sub is None:
            self._task_sub = await self._synapse.subscribe(
                self._subject(SignalType.TASK),
                self._on_task,
            )
        await self._refresh_routed_sub()
        await self._ensure_inbound_sub(SignalType.TASK_AWARDED)
        await self._ensure_inbound_sub(SignalType.DISCOVER)
        await self._ensure_inbound_sub(SignalType.STOP)
        if self._auto_bid:
            await self._ensure_inbound_sub(SignalType.TASK_OFFER)
        await self._mirror_to_store(axon, status="registered")
        await self._emit_register(axon)
        await axon._on_register_emitted()

    async def _refresh_routed_sub(self) -> None:
        """(Re)subscribe the capability-routed TASK subscription so its
        queue group matches the *current* aggregate cap profile. Called
        on live attach/detach  -  a stale group would load-balance this
        Dendrite into the wrong population (or none)."""
        qgroup = self._cap_queue_group()
        if self._routed_task_sub is not None:
            try:
                await self._routed_task_sub.unsubscribe()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Dendrite failed to unsubscribe routed TASK during "
                    "refresh: %s", exc,
                )
            self._routed_task_sub = None
        if qgroup is not None:
            self._routed_task_sub = await self._synapse.subscribe(
                self._routed_subject(),
                self._on_task,
                queue_group=qgroup,
            )

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
        engram._dendrite = self

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
        engram._dendrite = None

    @property
    def engrams(self) -> dict[str, Engram]:
        return dict(self._engrams)

    def attach_effector(self, effector: Effector) -> None:
        """Mount an Effector on this Dendrite.

        After attachment, the Dendrite subscribes to TOOL_CALL signals
        addressed to ``effector.effector_id`` or matching
        ``effector.effector_kind`` and dispatches them to the attached
        instance, publishing the TOOL_RESULT reply. The Effector still
        owns its backend lifecycle - the Dendrite calls ``connect()`` on
        start and ``close()`` on stop/detach.

        Multiple Effectors may share an ``effector_kind``; addressed
        routing by ``effector_id`` still works because the receiving
        Dendrite indexes both.
        """
        if effector.effector_id in self._effectors:
            raise ValueError(
                f"Dendrite already hosts an Effector with effector_id="
                f"{effector.effector_id!r}"
            )
        self._effectors[effector.effector_id] = effector
        self._effector_kind_index.setdefault(
            effector.effector_kind, []
        ).append(effector.effector_id)
        effector._dendrite = self

    async def detach_effector(self, effector_id: str) -> None:
        """Remove a hosted Effector. Closes its backend."""
        effector = self._effectors.get(effector_id)
        if effector is None:
            raise ValueError(
                f"Dendrite has no Effector with effector_id={effector_id!r}"
            )
        try:
            await effector.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Dendrite: Effector %s close raised on detach: %s",
                effector_id, exc,
            )
        bucket = self._effector_kind_index.get(effector.effector_kind, [])
        if effector_id in bucket:
            bucket.remove(effector_id)
        if not bucket:
            self._effector_kind_index.pop(effector.effector_kind, None)
        del self._effectors[effector_id]
        effector._dendrite = None

    @property
    def effectors(self) -> dict[str, Effector]:
        return dict(self._effectors)

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
        elif self._running:
            # Axons remain: the aggregate cap profile changed, so the
            # routed queue group must be re-keyed.
            await self._refresh_routed_sub()

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
            sig_neuron = sig.directed.id if sig.directed else None
            if neuron is not None and sig_neuron != neuron:
                return
            if trace_id is not None and sig.trace_id != trace_id:
                return
            if capability is not None:
                # A TASK_OFFER is a broadcast: it carries its required
                # capabilities in ``payload["capabilities"]`` and has no
                # directed neuron, so the neuron-capability lookup below
                # never matches. Narrow against the offer's requested set
                # instead (an offer with no capabilities is open to all).
                if sig.type is SignalType.TASK_OFFER:
                    requested = sig.payload.get("capabilities") or []
                    if requested and capability not in requested:
                        return
                elif not await self._neuron_has_capability(
                    sig_neuron, capability
                ):
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
                # Late registration (decorating after start()). The
                # subscription is established asynchronously; track the
                # task so failures are logged rather than swallowed, and
                # so ensure_subscribed() can await completion when the
                # caller needs the subscription to be live before the
                # next emit.
                task = asyncio.create_task(
                    self._ensure_inbound_sub(signal_type)
                )
                self._pending_sub_tasks.add(task)

                def _done(t: asyncio.Task[None]) -> None:
                    self._pending_sub_tasks.discard(t)
                    if not t.cancelled() and t.exception() is not None:
                        logger.error(
                            "Dendrite: late subscription for %s failed: %s",
                            signal_type.value, t.exception(),
                        )
                task.add_done_callback(_done)
            return fn
        return decorator

    async def ensure_subscribed(self, *types: SignalType) -> None:
        """Await until inbound subscriptions exist for ``types``.

        Decorating a running Dendrite establishes the subscription
        asynchronously; a Signal emitted immediately afterwards can race
        it. ``await d.ensure_subscribed(SignalType.X)`` removes the race
        deterministically. Idempotent  -  already-subscribed types are
        no-ops.
        """
        for t in types:
            await self._ensure_inbound_sub(t)

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

    # -- Generic handler registration --------------------------------------

    def on_signal(
        self,
        signal_type: SignalType,
        fn: SignalHandler | None = None,
        *,
        neuron: str | None = None,
        capability: str | None = None,
        trace_id: str | None = None,
    ) -> Any:
        """Register a handler for *any* SignalType.

        The generic escape hatch behind every named ``on_*`` decorator  -
        new protocol types are observable the day they exist, without
        waiting for named sugar::

            @d.on_signal(SignalType.FINAL)
            async def done(sig): ...

        Supports the same ``neuron=`` / ``capability=`` / ``trace_id=``
        filters as the named decorators.
        """
        return self._decorator_or_call(fn, self._on(
            signal_type,
            neuron=neuron, capability=capability, trace_id=trace_id,
        ))

    def on_final(self, fn: SignalHandler | None = None, *, neuron: str | None = None, capability: str | None = None, trace_id: str | None = None) -> Any:
        """Register a handler fired on FINAL  -  workflow conclusion."""
        return self._decorator_or_call(fn, self._on(
            SignalType.FINAL,
            neuron=neuron, capability=capability, trace_id=trace_id,
        ))

    def on_task_signal(self, fn: SignalHandler | None = None, *, neuron: str | None = None, capability: str | None = None, trace_id: str | None = None) -> Any:
        """Observe inbound TASKs (audit/logging). Observation only  -
        Axon routing happens on its own subscription and is unaffected
        by handlers registered here."""
        return self._decorator_or_call(fn, self._on(
            SignalType.TASK,
            neuron=neuron, capability=capability, trace_id=trace_id,
        ))

    def on_bid(self, fn: SignalHandler | None = None, *, neuron: str | None = None, capability: str | None = None, trace_id: str | None = None) -> Any:
        """Observe BIDs (market observability / auditing). dispatch_offer
        collects its own BIDs independently of handlers here."""
        return self._decorator_or_call(fn, self._on(
            SignalType.BID,
            neuron=neuron, capability=capability, trace_id=trace_id,
        ))

    def on_task_awarded(self, fn: SignalHandler | None = None, *, neuron: str | None = None, capability: str | None = None, trace_id: str | None = None) -> Any:
        """Observe TASK_AWARDED. The hosting Dendrite's own award-to-TASK
        synthesis is unaffected by handlers here."""
        return self._decorator_or_call(fn, self._on(
            SignalType.TASK_AWARDED,
            neuron=neuron, capability=capability, trace_id=trace_id,
        ))

    def on_task_declined(self, fn: SignalHandler | None = None, *, neuron: str | None = None, capability: str | None = None, trace_id: str | None = None) -> Any:
        """Register a handler fired on TASK_DECLINED  -  e.g. release a
        reservation made while bidding."""
        return self._decorator_or_call(fn, self._on(
            SignalType.TASK_DECLINED,
            neuron=neuron, capability=capability, trace_id=trace_id,
        ))

    def on_clarification_answer(self, fn: SignalHandler | None = None, *, neuron: str | None = None, capability: str | None = None, trace_id: str | None = None) -> Any:
        """Register a handler fired on CLARIFICATION_ANSWER  -  the
        discrete answer some Dendrite emitted via answer_clarification.
        Correlate by ``sig.parent_id == the CLARIFICATION's id`` (or use
        :meth:`await_decision` for the awaitable shape)."""
        return self._decorator_or_call(fn, self._on(
            SignalType.CLARIFICATION_ANSWER,
            neuron=neuron, capability=capability, trace_id=trace_id,
        ))

    def on_permission_decision(self, fn: SignalHandler | None = None, *, neuron: str | None = None, capability: str | None = None, trace_id: str | None = None) -> Any:
        """Register a handler fired on PERMISSION_DECISION  -  the discrete
        verdict some Dendrite emitted via grant_permission/deny_permission.
        Correlate by ``sig.parent_id == the PERMISSION's id`` (or use
        :meth:`await_decision` for the awaitable shape)."""
        return self._decorator_or_call(fn, self._on(
            SignalType.PERMISSION_DECISION,
            neuron=neuron, capability=capability, trace_id=trace_id,
        ))

    def on_recalled(self, fn: SignalHandler | None = None, *, neuron: str | None = None, capability: str | None = None, trace_id: str | None = None) -> Any:
        """Observe RECALLED responses (memory-traffic observability).
        EngramClient correlation is unaffected by handlers here."""
        return self._decorator_or_call(fn, self._on(
            SignalType.RECALLED,
            neuron=neuron, capability=capability, trace_id=trace_id,
        ))

    def on_imprinted(self, fn: SignalHandler | None = None, *, neuron: str | None = None, capability: str | None = None, trace_id: str | None = None) -> Any:
        """Observe IMPRINTED receipts (memory-traffic observability)."""
        return self._decorator_or_call(fn, self._on(
            SignalType.IMPRINTED,
            neuron=neuron, capability=capability, trace_id=trace_id,
        ))

    def on_recall_signal(self, fn: SignalHandler | None = None, *, neuron: str | None = None, capability: str | None = None, trace_id: str | None = None) -> Any:
        """Observe inbound RECALL requests. Observation only  -  hosted
        Engram routing happens before handlers fire and is unaffected."""
        return self._decorator_or_call(fn, self._on(
            SignalType.RECALL,
            neuron=neuron, capability=capability, trace_id=trace_id,
        ))

    def on_imprint_signal(self, fn: SignalHandler | None = None, *, neuron: str | None = None, capability: str | None = None, trace_id: str | None = None) -> Any:
        """Observe inbound IMPRINT requests. Observation only."""
        return self._decorator_or_call(fn, self._on(
            SignalType.IMPRINT,
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
            if self._auto_bid:
                # Default bidder: listen for TASK_OFFERs so stock workers
                # participate in offer/bid routing out of the box.
                await self._ensure_inbound_sub(SignalType.TASK_OFFER)
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
            # Terminal events are the saga commit point - an Engram host
            # must see FINAL/ERROR (and STOP, below) to commit or roll back
            # its per-trace journal even when it never dispatches.
            await self._ensure_inbound_sub(SignalType.FINAL)
            await self._ensure_inbound_sub(SignalType.ERROR)
            # Engrams are Synapse participants: announce each hosted Engram
            # with REGISTER (engram=True) so peers can learn it, and listen
            # for peer Engram registrations.
            await self._ensure_inbound_sub(SignalType.REGISTER)
            for engram in self._engrams.values():
                try:
                    await self._emit_engram_register(engram)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Dendrite: Engram %s REGISTER emit failed: %s",
                        engram.engram_id, exc,
                    )
                # Replay @engram.host.on_<signal> registrations declared on
                # the Engram itself - the memory-side twin of an Axon's
                # @axon.host.on_* replay on REGISTER.
                try:
                    await engram._on_hosted(self)
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "Dendrite: Engram %s host-proxy replay failed: %s",
                        engram.engram_id, exc,
                    )
        # Effector subscriptions. A Dendrite hosting Effectors listens for
        # TOOL_CALL and services it; the TOOL_RESULT reply is correlated
        # by parent_id on the caller side, same as RECALLED/IMPRINTED.
        if self._effectors:
            for effector in self._effectors.values():
                try:
                    await effector.connect()
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "Dendrite: Effector %s connect failed: %s",
                        effector.effector_id, exc,
                    )
            await self._ensure_inbound_sub(SignalType.TOOL_CALL)
            # Effectors are Synapse participants too: announce each hosted
            # Effector with REGISTER (role="effector") so peers - and Prism -
            # can learn it and classify it distinctly from a Neuron/Engram.
            await self._ensure_inbound_sub(SignalType.REGISTER)
            for effector in self._effectors.values():
                try:
                    await self._emit_effector_register(effector)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Dendrite: Effector %s REGISTER emit failed: %s",
                        effector.effector_id, exc,
                    )
                # Replay @effector.host.on_<signal> registrations declared
                # on the Effector itself - mirrors the Engram replay above.
                try:
                    await effector._on_hosted(self)
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "Dendrite: Effector %s host-proxy replay failed: %s",
                        effector.effector_id, exc,
                    )

        # Always listen for RECALLED/IMPRINTED  -  the Dendrite owns the
        # EngramClient's correlation table even when it hosts no Axons,
        # because a Cortex calls dendrite.recall/imprint directly.
        await self._ensure_inbound_sub(SignalType.RECALLED)
        await self._ensure_inbound_sub(SignalType.IMPRINTED)
        # ... and TOOL_RESULT, for the same reason: the Dendrite owns
        # the EffectorClient's correlation table even when it hosts no
        # Effectors, because Axons and Cortex code call call_tool.
        await self._ensure_inbound_sub(SignalType.TOOL_RESULT)

        for signal_type, handlers in self._handlers.items():
            if handlers:
                await self._ensure_inbound_sub(signal_type)

        if self._registry_store is not None:
            for mgmt_type in (SignalType.REGISTER, SignalType.DEREGISTER,
                              SignalType.HEARTBEAT):
                await self._ensure_inbound_sub(mgmt_type)

        # Every started Dendrite listens for STOP so it can cancel its share
        # of any trace it participates in.
        await self._ensure_inbound_sub(SignalType.STOP)

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

        # Close any in-flight op-Pathways (recall/imprint) so their awaiters
        # wake with EngramCancelled instead of hanging on a deadline.
        for op_pathway in list(self._op_pathways.values()):
            try:
                await op_pathway.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Op-Pathway close raised: %s", exc)
        self._op_pathways.clear()

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

        for task in list(self._pending_sub_tasks):
            task.cancel()
        self._pending_sub_tasks.clear()

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

        # In-flight engram I/O was already cancelled above by closing the
        # op-Pathways (recall/imprint awaiters wake with EngramCancelled).

        for engram in self._engrams.values():
            try:
                await engram.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Dendrite: Engram %s close raised on stop: %s",
                    engram.engram_id, exc,
                )

        for effector in self._effectors.values():
            try:
                await effector.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Dendrite: Effector %s close raised on stop: %s",
                    effector.effector_id, exc,
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
        max_age_s: float | None = None,
    ) -> list[NeuronRecord]:
        records = await self._require_store().list(
            capability=capability,
            include_deregistered=include_deregistered,
        )
        if max_age_s is not None:
            records = self._filter_fresh(records, max_age_s)
        return records

    async def find_neurons(
        self, *, capability: str | None = None,
        max_age_s: float | None = None,
    ) -> list[NeuronRecord]:
        """Live Neurons, optionally narrowed by ``capability``.

        ``max_age_s`` additionally drops records whose last heartbeat is
        older than the given age  -  a read-side freshness guard for
        callers that cannot rely on the background staleness sweep.
        """
        records = await self._require_store().list(
            capability=capability,
            include_deregistered=False,
        )
        if max_age_s is not None:
            records = self._filter_fresh(records, max_age_s)
        return records

    @staticmethod
    def _filter_fresh(
        records: list[NeuronRecord], max_age_s: float,
    ) -> list[NeuronRecord]:
        now = datetime.now(timezone.utc)
        fresh: list[NeuronRecord] = []
        for rec in records:
            seen = rec.last_heartbeat or rec.registered_at
            if seen is None:
                continue
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=timezone.utc)
            if (now - seen).total_seconds() <= max_age_s:
                fresh.append(rec)
        return fresh

    # ------------------------------------------------------------------
    # Outbound primitives
    # ------------------------------------------------------------------

    def _inherit_parent(self, parent_id: str | None) -> str | None:
        """Default ``parent_id`` from the ambient task context so a TASK
        dispatched from *inside* a running task links to that task instead
        of surfacing as an orphan trace. Mirrors how the recall / imprint /
        tool helpers inherit ``trace_context``. An explicit ``parent_id``
        always wins; outside any task context the ambient is ``None`` and
        dispatch behaves exactly as before."""
        if parent_id is not None:
            return parent_id
        amb = ambient_trace()
        return amb[1] if amb is not None else None

    async def dispatch_task(
        self, *, neuron: str | None = None, input: dict[str, Any],
        trace_id: str | None = None, parent_id: str | None = None,
        context_ref: str | None = None,
        capabilities: list[str] | None = None,
        finalize: bool = False,
        meta: dict[str, Any] | None = None,
    ) -> Signal:
        """Emit a TASK signal. Addressed (``neuron=...``) or capability-routed
        (``capabilities=[...]``)  -  at least one must be set.

        Addressed TASKs go on the broadcast TASK subject; the unique
        host filters by neuron_id and acts. Capability-routed TASKs go
        on the queue-grouped routed subject so the broker delivers them
        to exactly one Dendrite per matching cap profile.

        ``finalize=True`` tags the TASK so the handling worker Dendrite
        promotes a successful AGENT_OUTPUT to FINAL (terminal-handler
        finalize  -  see :meth:`dispatch`).

        Only orchestrator-role Dendrites may dispatch.
        """
        self._require_orchestrator("dispatch_task")
        if neuron is None and not capabilities:
            raise ValueError(
                "dispatch_task requires either neuron= (addressed) or "
                "capabilities=[...] (capability-routed)"
            )
        parent_id = self._inherit_parent(parent_id)
        sig = task_signal(
            trace_id=trace_id, parent_id=parent_id,
            directed=Directed(id=neuron) if neuron else None,
            input=input, context_ref=context_ref,
            capabilities=capabilities, finalize=finalize, meta=meta,
        )
        await self._publish_task(sig)
        return sig

    async def _publish_task(self, sig: Signal) -> None:
        """Publish a TASK to the correct subject for its routing mode.

        Addressed (``sig.directed.id`` set) → broadcast subject.
        Capability-routed (no directed.id, capabilities in payload) → routed
        subject (queue-grouped on receivers, once-only delivery within
        a matching cap profile).
        """
        if sig.directed and sig.directed.id:
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
        finalize: bool | None = None,
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
        capability set may pick it up. Delivery is exactly-once within a
        queue group (identical cap profiles) but **at-least-once across
        heterogeneous groups**: when different Dendrites declare different
        but overlapping cap profiles, more than one may consume the same
        routed TASK. Use :meth:`dispatch_offer` (TASK_OFFER / BID /
        TASK_AWARDED) when overlapping profiles exist and the work must
        be claimed atomically.

        ``scope="all"`` (default) delivers every PATHWAY_TYPES Signal on
        the trace to the Pathway; ``scope="terminal"`` filters to FINAL /
        ERROR / CLARIFICATION / PERMISSION only  -  the decentralised pattern
        where intermediate orchestration is handled by other Dendrites and the
        Cortex only wakes for terminal events or a needed decision.

        ``finalize`` controls terminal-handler finalization: a tagged TASK
        makes the worker Dendrite that ran the Axon promote a successful
        AGENT_OUTPUT by also emitting FINAL on the trace. Default
        (``None``) resolves to True exactly when ``scope="terminal"``  -
        a default Axon never emits FINAL itself, so a terminal-scoped
        Pathway would otherwise never resolve against stock workers. Pass
        ``finalize=False`` to opt out (e.g. another peer owns FINAL), or
        ``finalize=True`` to get a FINAL even on an all-scope dispatch.

        The Pathway auto-closes on the first FINAL or ERROR Signal;
        :meth:`stop` closes any still-open Pathways.
        """
        self._require_orchestrator("dispatch")
        if neuron is None and not capabilities:
            raise ValueError(
                "dispatch requires either neuron= (addressed) or "
                "capabilities=[...] (capability-routed)"
            )
        if finalize is None:
            finalize = scope == "terminal"
        tid = trace_id or new_trace_id()
        parent_id = self._inherit_parent(parent_id)

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
            trace_id=tid, parent_id=parent_id,
            directed=Directed(id=neuron) if neuron else None,
            input=input, context_ref=context_ref,
            capabilities=capabilities, finalize=finalize, meta=meta,
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
        finalize: bool | None = None,
        retry: RetryStrategy | None = None,
    ) -> Signal:
        """Sync-shape sugar: dispatch, block until first terminal Signal,
        close the Pathway, return the Signal.

        Equivalent to::

            async with await orch.dispatch(...) as pw:
                return await pw.wait(timeout_s=timeout_s)

        Works for both addressed (``neuron=``) and capability-routed
        (``capabilities=[...]``) dispatch, and in centralized or
        decentralized topologies. Use ``scope="terminal"`` to wait only
        for FINAL / ERROR / CLARIFICATION / PERMISSION.

        Raises :class:`asyncio.TimeoutError` if ``timeout_s`` elapses
        before a terminal Signal arrives, and
        :class:`cosmonapse.pathway.PathwayClosedError` if the Pathway
        is closed (e.g. by Dendrite shutdown) before any matching
        Signal arrives.
        """
        if retry is not None:
            return await self._dispatch_with_retry(
                retry=retry, neuron=neuron, input=input, timeout_s=timeout_s,
                trace_id=trace_id, parent_id=parent_id, context_ref=context_ref,
                capabilities=capabilities, meta=meta, scope=scope,
                finalize=finalize,
            )
        pathway = await self.dispatch(
            neuron=neuron, input=input,
            trace_id=trace_id, parent_id=parent_id,
            context_ref=context_ref, capabilities=capabilities,
            meta=meta, scope=scope, finalize=finalize,
        )
        async with pathway as pw:
            return await pw.wait(timeout_s=timeout_s)

    async def run_with_retry(
        self,
        *,
        retry: RetryStrategy,
        neuron: str | None = None,
        input: dict[str, Any],
        timeout_s: float | None = 30.0,
        trace_id: str | None = None,
        parent_id: str | None = None,
        context_ref: str | None = None,
        capabilities: list[str] | None = None,
        meta: dict[str, Any] | None = None,
        scope: str = "all",
        finalize: bool | None = None,
    ) -> Signal:
        """Dispatch and wait, retrying per ``retry`` until a non-retryable
        outcome or attempts are exhausted. Returns the resolved Signal
        (FINAL / AGENT_OUTPUT / CLARIFICATION / PERMISSION, or a final ERROR);
        re-raises the last exception when every attempt timed out."""
        return await self._dispatch_with_retry(
            retry=retry, neuron=neuron, input=input, timeout_s=timeout_s,
            trace_id=trace_id, parent_id=parent_id, context_ref=context_ref,
            capabilities=capabilities, meta=meta, scope=scope, finalize=finalize,
        )

    async def _safe_stop(self, trace_id: str, retry: RetryStrategy) -> None:
        try:
            await self.emit_stop(
                trace_id=trace_id, rollback=retry.rollback_on_retry,
                reason=retry.reason,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "run_with_retry: preemptive STOP of %s failed: %s",
                trace_id, exc,
            )

    async def _dispatch_with_retry(
        self,
        *,
        retry: RetryStrategy,
        neuron: str | None,
        input: dict[str, Any],
        timeout_s: float | None,
        trace_id: str | None,
        parent_id: str | None,
        context_ref: str | None,
        capabilities: list[str] | None,
        meta: dict[str, Any] | None,
        scope: str,
        finalize: bool | None,
    ) -> Signal:
        attempts = retry.max_attempts
        outcome: object = None
        for attempt in range(attempts):
            tid = (
                trace_id if (trace_id and not retry.new_trace)
                else new_trace_id()
            )
            per_timeout = (
                retry.timeout_s if retry.timeout_s is not None else timeout_s
            )
            attempt_meta = {**(meta or {}), "attempt": attempt}
            try:
                pathway = await self.dispatch(
                    neuron=neuron, input=input, trace_id=tid,
                    parent_id=parent_id, context_ref=context_ref,
                    capabilities=capabilities, meta=attempt_meta,
                    scope=scope, finalize=finalize,
                )
                async with pathway as pw:
                    sig = await pw.wait(timeout_s=per_timeout)
            except (asyncio.TimeoutError, PathwayClosedError) as exc:
                outcome = exc
            else:
                outcome = sig

            should_retry = bool(retry.retry_on(outcome))
            if not should_retry:
                if isinstance(outcome, Signal):
                    return outcome
                assert isinstance(outcome, BaseException)
                raise outcome

            # Retryable: preempt the abandoned attempt so a stalled worker
            # (and its half-finished Engram writes) can't outlive the retry.
            if retry.new_trace:
                await self._safe_stop(tid, retry)

            if attempt + 1 >= attempts:
                if isinstance(outcome, Signal):
                    return outcome
                assert isinstance(outcome, BaseException)
                raise outcome

            if retry.on_retry is not None:
                try:
                    retry.on_retry(attempt, outcome)
                except Exception:  # noqa: BLE001
                    logger.exception("run_with_retry: on_retry hook raised")

            try:
                delay = float(retry.backoff(attempt) or 0.0)
            except Exception:  # noqa: BLE001
                delay = 0.0
            if delay > 0:
                await asyncio.sleep(delay)

        raise RuntimeError("run_with_retry: exhausted attempts unexpectedly")

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
        finalize: bool | None = None,
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
            meta=meta, scope=scope, finalize=finalize,
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
        finalize: bool | None = None,
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

        ``finalize`` follows the same terminal-handler-finalize rule as
        :meth:`dispatch` (default: True when ``scope="terminal"``); the
        tag rides the TASK_AWARDED into the TASK the winner's Dendrite
        synthesises.

        Raises ``TimeoutError`` if no BID arrives within ``deadline_ms``.
        Only orchestrator-role Dendrites may call this.
        """
        self._require_orchestrator("dispatch_offer")
        if select not in ("first_bid", "lowest_cost", "highest_confidence"):
            raise ValueError(
                f"select must be one of 'first_bid' / 'lowest_cost' / "
                f"'highest_confidence', got {select!r}"
            )

        if finalize is None:
            finalize = scope == "terminal"
        tid = trace_id or new_trace_id()
        parent_id = self._inherit_parent(parent_id)
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
            b_neuron = b.directed.id if b.directed else None
            try:
                await self.emit(task_declined_signal(
                    trace_id=tid, parent_id=b.id,
                    directed=Directed(id=b_neuron) if b_neuron else None,
                    reason="not selected",
                ))
            except Exception as exc:
                logger.warning(
                    "dispatch_offer: TASK_DECLINED emit failed for %s: %s",
                    b_neuron, exc,
                )

        # Award. The winning Axon's Dendrite will handle it via
        # _on_task_awarded -> Axon.handle_task.
        winner_neuron = winner.directed.id if winner.directed else None
        awarded = task_awarded_signal(
            trace_id=tid, parent_id=winner.id,
            directed=Directed(id=winner_neuron) if winner_neuron else None,
            input=input,
            winning_bid={
                k: winner.payload.get(k)
                for k in ("cost", "eta_ms", "confidence")
                if k in winner.payload
            },
            context_ref=context_ref,
            finalize=finalize,
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
            directed=Directed(id=neuron), cost=cost, eta_ms=eta_ms,
            confidence=confidence, meta=meta,
        )
        # _publish bypasses the orchestrator guard in emit()  -  a worker
        # bidding is announcing capability, not dispatching work.
        await self._publish(sig)
        return sig

    async def _maybe_auto_bid(self, offer: Signal) -> None:
        """Bid on a TASK_OFFER with the first hosted Axon whose capability
        set covers the offer's request. No-op when nothing matches. An
        offer with no capability filter is open to any Axon."""
        requested = set(offer.payload.get("capabilities") or [])
        for axon in self._axons.values():
            if requested and not requested.issubset(set(axon.capabilities)):
                continue
            try:
                await self.bid(
                    offer, neuron=axon.neuron_id,
                    cost=0.0, confidence=1.0,
                    meta={"auto_bid": True},
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Dendrite: auto-bid failed for %s: %s",
                    axon.neuron_id, exc,
                )
            return

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

    # -- per-operation (request/reply) Pathways --------------------------
    # The generic correlation primitive: a Pathway keyed on the issuing
    # request's id, matched against inbound ``parent_id``. EngramClient is
    # the first wrapper over it (recall/imprint); any future request/reply
    # client (permission, clarification, ...) can reuse it unchanged.

    def _open_op_pathway(
        self, *, op_id: str, trace_id: str, scope: str = "all",
    ) -> Pathway:
        """Open a per-operation Pathway correlated by ``op_id``.

        Inbound Signals whose ``parent_id == op_id`` are delivered to it by
        :meth:`_dispatch_inbound`. ``trace_id`` is carried only for lifecycle
        grouping so the operation is cancelled when its parent TASK ends.
        The Dendrite already subscribes to RECALLED/IMPRINTED in ``start()``,
        so no extra subscription is needed here.
        """
        pathway = Pathway(
            trace_id=trace_id, parent_id=op_id, role="originator",
            on_close=self._on_op_pathway_close, scope=scope,
        )
        self._op_pathways[op_id] = pathway
        return pathway

    async def _on_op_pathway_close(self, pathway: Pathway) -> None:
        if pathway.parent_id is not None:
            self._op_pathways.pop(pathway.parent_id, None)

    async def _cancel_op_pathways(self, trace_id: str) -> None:
        """Close every in-flight op-Pathway on ``trace_id``. Awaiting clients
        (recall/imprint) wake with PathwayClosedError, which they surface as
        EngramCancelled. Snapshot first - close() mutates ``_op_pathways``."""
        for pw in [p for p in self._op_pathways.values() if p.trace_id == trace_id]:
            try:
                await pw.close()
            except Exception as exc:  # noqa: BLE001  -  teardown must not raise
                logger.warning("Dendrite: op-Pathway close raised: %s", exc)

    async def emit_final(self, *, trace_id: str, parent_id: str, result: Any,
                         neuron: str | None = None, meta: dict[str, Any] | None = None) -> Signal:
        """``neuron=`` attributes the FINAL to the producing Neuron instead
        of this Dendrite - the attribution terminal-handler promotion uses,
        so observers keep the lineage TASK -> AGENT_OUTPUT -> FINAL."""
        sig = final_signal(trace_id=trace_id, parent_id=parent_id,
                           directed=Directed(id=neuron or self.dendrite_id), result=result, meta=meta)
        await self.emit(sig)
        return sig

    async def emit_error(self, *, trace_id: str, parent_id: str, code: str, message: str,
                         recoverable: bool = False, neuron: str | None = None,
                         meta: dict[str, Any] | None = None) -> Signal:
        sig = error_signal(trace_id=trace_id, parent_id=parent_id,
                           directed=Directed(id=neuron or self.dendrite_id), code=code, message=message,
                           recoverable=recoverable, meta=meta)
        await self.emit(sig)
        return sig

    # -- Cognition emit helpers ------------------------------------------

    async def emit_plan(self, *, trace_id: str, parent_id: str, steps: list[Any],
                        rationale: str | None = None, neuron: str | None = None, meta: dict[str, Any] | None = None) -> Signal:
        sig = plan_signal(
            trace_id=trace_id, parent_id=parent_id,
            directed=Directed(id=neuron or self.dendrite_id),
            steps=steps, rationale=rationale, meta=meta,
        )
        await self.emit(sig)
        return sig

    async def emit_thought_delta(self, *, trace_id: str, parent_id: str, delta: str,
                                 seq: int | None = None, neuron: str | None = None, meta: dict[str, Any] | None = None) -> Signal:
        sig = thought_delta_signal(
            trace_id=trace_id, parent_id=parent_id,
            directed=Directed(id=neuron or self.dendrite_id),
            delta=delta, seq=seq, meta=meta,
        )
        await self.emit(sig)
        return sig

    async def emit_tool_call(self, *, trace_id: str, parent_id: str, tool: str, args: dict[str, Any],
                             call_id: str | None = None, neuron: str | None = None, meta: dict[str, Any] | None = None) -> Signal:
        sig = tool_call_signal(
            trace_id=trace_id, parent_id=parent_id,
            directed=Directed(id=neuron or self.dendrite_id),
            tool=tool, args=args, call_id=call_id, meta=meta,
        )
        await self.emit(sig)
        return sig

    async def emit_tool_result(self, *, trace_id: str, parent_id: str, tool: str,
                               result: Any = None, error: Any = None, call_id: str | None = None,
                               neuron: str | None = None, meta: dict[str, Any] | None = None) -> Signal:
        sig = tool_result_signal(
            trace_id=trace_id, parent_id=parent_id,
            directed=Directed(id=neuron or self.dendrite_id),
            tool=tool, result=result, error=error, call_id=call_id, meta=meta,
        )
        await self.emit(sig)
        return sig

    async def emit_memory_append(self, *, trace_id: str, parent_id: str, key: str, value: Any,
                                 neuron: str | None = None, meta: dict[str, Any] | None = None) -> Signal:
        sig = memory_append_signal(
            trace_id=trace_id, parent_id=parent_id,
            directed=Directed(id=neuron or self.dendrite_id),
            key=key, value=value, meta=meta,
        )
        await self.emit(sig)
        return sig

    async def emit_critique(self, *, trace_id: str, parent_id: str, target_event_id: str,
                            issues: list[Any], verdict: str, neuron: str | None = None, meta: dict[str, Any] | None = None) -> Signal:
        sig = critique_signal(
            trace_id=trace_id, parent_id=parent_id,
            directed=Directed(id=neuron or self.dendrite_id),
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
            directed=Directed(id=neuron or self.dendrite_id),
            reason=reason, target=target, context=context, meta=meta,
        )
        await self.emit(sig)
        return sig

    async def emit_consensus(self, *, trace_id: str, parent_id: str, members: list[str], verdict: str,
                             votes: dict[str, Any] | None = None, neuron: str | None = None, meta: dict[str, Any] | None = None) -> Signal:
        sig = consensus_signal(
            trace_id=trace_id, parent_id=parent_id,
            directed=Directed(id=neuron or self.dendrite_id),
            members=members, verdict=verdict, votes=votes, meta=meta,
        )
        await self.emit(sig)
        return sig

    async def emit_context_sync(self, *, trace_id: str, parent_id: str, snapshot: dict[str, Any],
                                version: str | None = None, neuron: str | None = None, meta: dict[str, Any] | None = None) -> Signal:
        sig = context_sync_signal(
            trace_id=trace_id, parent_id=parent_id,
            directed=Directed(id=neuron or self.dendrite_id),
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
        target = neuron or (signal.directed.id if signal.directed else None)
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
                "from":    signal.directed.id,
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
                "from": signal.directed.id if signal.directed else None,
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
        target = neuron or (signal.directed.id if signal.directed else None)
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
            directed=Directed(id=self.dendrite_id),
            reason=reason,
            ttl_ms=ttl_ms,
            meta=meta,
        )
        await self._publish(sig)
        return sig

    async def await_decision(
        self,
        request: Signal,
        *,
        timeout_s: float | None = 30.0,
    ) -> Signal:
        """Await the discrete answer to a CLARIFICATION or PERMISSION.

        Opens a per-operation Pathway keyed on ``request.id`` and resolves
        on the first CLARIFICATION_ANSWER (for a CLARIFICATION request) or
        PERMISSION_DECISION (for a PERMISSION request) whose ``parent_id``
        matches. The awaitable counterpart to the
        :meth:`on_clarification_answer` / :meth:`on_permission_decision`
        decorators  -  same machinery EngramClient uses for RECALL/IMPRINT.

        Typical use: an observer Dendrite saw a PERMISSION request on the
        bus and wants the eventual verdict (e.g. to imprint it)::

            verdict = await d.await_decision(permission_sig, timeout_s=60)

        Raises ``DendriteProtocolError`` for other request types,
        ``asyncio.TimeoutError`` on deadline, and ``PathwayClosedError``
        if the Dendrite stops first.
        """
        if request.type is SignalType.CLARIFICATION:
            expected = SignalType.CLARIFICATION_ANSWER
        elif request.type is SignalType.PERMISSION:
            expected = SignalType.PERMISSION_DECISION
        else:
            raise DendriteProtocolError(
                f"await_decision expects a CLARIFICATION or PERMISSION "
                f"signal, got {request.type.value!r}"
            )
        await self._ensure_inbound_sub(expected)
        # The answer may already have flown by (in-process synapses deliver
        # the whole request->answer chain inside the original publish).
        # Serve and consume the cached copy if so.
        cached = self._recent_decisions.pop(request.id, None)
        if cached is not None and cached.type is expected:
            return cached
        pathway = self._open_op_pathway(
            op_id=request.id, trace_id=request.trace_id,
        )
        try:
            return await pathway.wait_for(expected, timeout_s=timeout_s)
        finally:
            await pathway.close()

    async def answer_clarification(
        self,
        request: Signal,
        *,
        answer: Any,
        meta: dict[str, Any] | None = None,
    ) -> Signal:
        """Answer a CLARIFICATION with a discrete CLARIFICATION_ANSWER
        signal (``parent_id`` = the request's id). Consumers pick it up
        via :meth:`on_clarification_answer` or :meth:`await_decision`.
        Distinct from :meth:`respond_to_clarification`, which closes the
        loop by re-dispatching a TASK carrying the answer so the asking
        Neuron runs again."""
        if request.type is not SignalType.CLARIFICATION:
            raise DendriteProtocolError(
                f"answer_clarification expects a CLARIFICATION signal, got "
                f"{request.type.value!r}"
            )
        sig = clarification_answer_signal(
            trace_id=request.trace_id,
            parent_id=request.id,
            answer=answer,
            directed=Directed(id=self.dendrite_id),
            meta=meta,
        )
        await self._publish(sig)
        return sig

    async def emit(self, signal: Signal) -> None:
        """Emit a synapse-side Signal (orchestration).

        Funnel for every public emit_* helper (emit_final / emit_error /
        emit_plan / emit_critique / ...). Subject to two guards:

        * Role guard - only TASK initiation is gated (``_ROLE_GATED_TYPES``):
          a non-orchestrator may not emit TASK. Every other synapse-side
          signal - cognition, replies, and RECALL / IMPRINT - is emitted
          regardless of role, so memory and other interactions run over the
          generic dispatch + Pathway path from any Dendrite. Worker Axons
          still publish AGENT_OUTPUT / CLARIFICATION / ERROR via the private
          ``_publish`` path, and ``bid()`` likewise bypasses this method.
        * Type guard - only SYNAPSE_TYPES are accepted; AXON_TYPES
          (REGISTER / HEARTBEAT / DEREGISTER / AGENT_OUTPUT / ...) must
          go through the internal management paths, not this method.
        """
        if signal.type in self._ROLE_GATED_TYPES:
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
        # Dedupe concurrent calls on the in-flight attempt, not the completed
        # subscription - otherwise two racing callers each subscribe and
        # every handler fires twice per signal. The first caller subscribes
        # inline (no extra event-loop hop); racers wait on the Event and
        # re-check, retrying only if the first attempt failed.
        while signal_type not in self._inbound_subs:
            evt = self._inflight_subs.get(signal_type)
            if evt is not None:
                await evt.wait()
                continue
            evt = asyncio.Event()
            self._inflight_subs[signal_type] = evt
            try:
                sub = await self.subscribe(signal_type, self._dispatch_inbound)
                self._inbound_subs[signal_type] = sub
            finally:
                self._inflight_subs.pop(signal_type, None)
                evt.set()

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
        target = task.directed.id if task.directed else None
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

        # Run the neuron in a child task registered under this trace so a
        # STOP on the trace can cancel exactly this work (cooperative
        # asyncio cancellation - the neuron_fn must clean up, not swallow,
        # CancelledError).
        child = asyncio.ensure_future(axon.handle_task(task))
        self._register_trace_task(task.trace_id, child)
        try:
            reply = await child
        except asyncio.CancelledError:
            if child.cancelled():
                # STOP cancelled the neuron mid-flight; the STOPPED ack
                # emitted by _on_stop covers it, so publish no reply.
                logger.info(
                    "Dendrite: TASK on trace %s cancelled by STOP",
                    task.trace_id,
                )
                return
            raise
        except Exception as exc:
            logger.exception("Dendrite: Axon %s raised unexpectedly", target)
            reply = error_signal(
                trace_id=task.trace_id, parent_id=task.id,
                directed=Directed(id=target), code="AXON_EXCEPTION",
                message=str(exc), recoverable=False,
            )
        finally:
            self._unregister_trace_task(task.trace_id, child)
        await self._publish(reply)

        # Terminal-handler finalize: when the dispatching side tagged the
        # TASK (payload.finalize  -  set automatically by
        # dispatch(scope="terminal")), promote a successful AGENT_OUTPUT by
        # also emitting FINAL so terminal-scoped Pathways resolve against
        # default workers. Only AGENT_OUTPUT is promoted: CLARIFICATION /
        # PERMISSION pause the workflow awaiting a decision, and ERROR is
        # already terminal. The FINAL is parented to the AGENT_OUTPUT and
        # attributed to the producing Neuron so observers keep the lineage
        # TASK -> AGENT_OUTPUT -> FINAL.
        if (
            reply.type is SignalType.AGENT_OUTPUT
            and task.payload.get("finalize")
        ):
            try:
                await self._publish(final_signal(
                    trace_id=reply.trace_id,
                    parent_id=reply.id,
                    directed=Directed(id=target),
                    result=reply.payload.get("output", {}),
                ))
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "Dendrite: terminal-handler FINAL publish failed "
                    "for %s: %s", target, exc,
                )

    # ------------------------------------------------------------------
    # Workflow control: STOP / STOPPED
    # ------------------------------------------------------------------

    def _register_trace_task(self, trace_id: str, task: "asyncio.Task[Any]") -> None:
        self._trace_tasks.setdefault(trace_id, set()).add(task)

    def _unregister_trace_task(self, trace_id: str, task: "asyncio.Task[Any]") -> None:
        tasks = self._trace_tasks.get(trace_id)
        if tasks is not None:
            tasks.discard(task)
            if not tasks:
                self._trace_tasks.pop(trace_id, None)

    async def _on_stop(self, signal: Signal) -> None:
        """React to an inbound STOP. Self-selects by trace_id: cancels local
        neuron work + engram I/O on the trace, optionally rolls back hosted
        Engrams, closes the trace's Pathway, and acks with STOPPED."""
        trace_id = signal.trace_id
        if not trace_id:
            return
        rollback = bool((signal.payload or {}).get("rollback"))
        cancelled = 0
        compensated = 0
        did_work = False

        # 1. Cancel in-flight neuron handler tasks on this trace.
        tasks = self._trace_tasks.pop(trace_id, None)
        if tasks:
            for t in list(tasks):
                if not t.done():
                    t.cancel()
                    cancelled += 1
            did_work = True

        # 2. Cancel in-flight engram op I/O (surfaces as EngramCancelled to
        #    any awaiting recall/imprint - same path FINAL/ERROR already use).
        try:
            await self._cancel_op_pathways(trace_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Dendrite: STOP op-pathway cancel failed for %s: %s",
                trace_id, exc,
            )

        # 3. Roll back (or just discard) each hosted Engram's saga journal.
        for engram in list(self._engrams.values()):
            try:
                if rollback:
                    n = await engram.compensate(trace_id)
                    if n:
                        compensated += n
                        did_work = True
                else:
                    await engram.commit(trace_id)
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "Dendrite: Engram %s STOP handling failed: %s",
                    engram.engram_id, exc,
                )

        # 4. Close any open Pathway for this trace so awaiters unblock.
        pw = self._pathways.get(trace_id)
        if pw is not None and not pw.closed:
            did_work = True
            try:
                await pw.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Dendrite: STOP pathway close failed for %s: %s",
                    trace_id, exc,
                )

        # 5. Ack only if this Dendrite had a stake in the trace, so idle
        #    peers that received the broadcast stay quiet.
        if did_work:
            try:
                await self._publish(stopped_signal(
                    trace_id=trace_id, parent_id=signal.id,
                    node=self._namespace, rolled_back=rollback,
                    cancelled=cancelled, compensated=compensated,
                ))
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "Dendrite: STOPPED publish failed for %s: %s", trace_id, exc,
                )

    async def emit_stop(
        self, *, trace_id: str, rollback: bool = False,
        reason: str | None = None,
    ) -> Signal:
        """Broadcast a STOP for ``trace_id`` (orchestrator-gated).

        Returns the emitted STOP Signal. Best-effort and idempotent: STOP is
        fire-and-forget, so a peer that never saw it simply isn't stopped."""
        self._require_orchestrator("emit_stop")
        # Ensure our own loopback STOP is handled (a Dendrite that both
        # originates and hosts work must react to its own STOP).
        await self._ensure_inbound_sub(SignalType.STOP)
        sig = stop_signal(trace_id=trace_id, rollback=rollback, reason=reason)
        await self._publish(sig)
        return sig

    async def stop_trace(
        self, trace_id: str, *, rollback: bool = False,
        reason: str | None = None, collect_acks: bool = False,
        timeout_s: float = 1.0,
    ) -> list[Signal]:
        """Stop a whole workflow. Thin wrapper over :meth:`emit_stop`.

        With ``collect_acks=True`` opens a short-lived STOPPED subscription
        and returns the acks seen within ``timeout_s`` (best effort)."""
        if not collect_acks:
            await self.emit_stop(
                trace_id=trace_id, rollback=rollback, reason=reason,
            )
            return []

        acks: list[Signal] = []

        async def _collect(sig: Signal) -> None:
            if sig.trace_id == trace_id:
                acks.append(sig)

        self._handlers.setdefault(SignalType.STOPPED, []).append(_collect)
        await self._ensure_inbound_sub(SignalType.STOPPED)
        try:
            await self.emit_stop(
                trace_id=trace_id, rollback=rollback, reason=reason,
            )
            await asyncio.sleep(timeout_s)
        finally:
            handlers = self._handlers.get(SignalType.STOPPED, [])
            if _collect in handlers:
                handlers.remove(_collect)
        return acks

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
        neuron_kind = getattr(axon, "neuron_kind", "neuron") or "neuron"
        await self._publish(register_signal(
            directed=Directed(
                id=axon.neuron_id,
                type=neuron_kind,
                capabilities=list(axon.capabilities),
            ),
            capabilities=axon.capabilities,
            version=axon.version,
            role="neuron",
        ))

    async def _emit_engram_register(self, engram: Engram) -> None:
        """Announce a hosted Engram on the Synapse via REGISTER.

        ``directed.id`` = engram_id, ``directed.type`` = engram_kind,
        ``directed.capabilities`` = the Engram's capabilities; the
        ``engram=True`` flag tells receivers to record it as an Engram
        registration rather than a Neuron.
        """
        caps = list(getattr(engram, "capabilities", []) or [])
        await self._publish(register_signal(
            directed=Directed(
                id=engram.engram_id,
                type=engram.engram_kind,
                capabilities=caps,
            ),
            capabilities=caps,
            version=getattr(engram, "version", None),
            engram=True,
            role="engram",
        ))

    async def _emit_effector_register(self, effector: Effector) -> None:
        """Announce a hosted Effector on the Synapse via REGISTER.

        ``directed.id`` = effector_id, ``directed.type`` = effector_kind,
        ``directed.capabilities`` = the Effector's capabilities; the
        ``role="effector"`` payload field tells receivers (Dendrite
        registry, Prism, doppler) to classify it distinctly from a Neuron
        or Engram - mirrors ``_emit_engram_register`` exactly, one level
        down the ABC hierarchy (Neurons think, Engrams remember, Effectors
        act).
        """
        caps = list(getattr(effector, "capabilities", []) or [])
        await self._publish(register_signal(
            directed=Directed(
                id=effector.effector_id,
                type=effector.effector_kind,
                capabilities=caps,
            ),
            capabilities=caps,
            version=getattr(effector, "version", None),
            role="effector",
        ))

    async def _emit_deregister(self, axon: Axon, *, reason: str | None) -> None:
        await self._publish(deregister_signal(
            directed=Directed(id=axon.neuron_id), reason=reason,
        ))

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
                        heartbeat_signal(directed=Directed(id=axon.neuron_id)),
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
            if self._registry_store is not None and self._stale_after_s > 0:
                try:
                    await self._sweep_stale_neurons(now)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Dendrite: stale-neuron sweep failed: %s", exc,
                    )
            await self._fire_refresh(RefreshEvent(reason="heartbeat"))

    async def _sweep_stale_neurons(self, now: datetime) -> None:
        """Mark Neurons deregistered when their last_heartbeat is older
        than ``stale_after_s``. Own hosted Axons were touched immediately
        before the sweep, so they never qualify. Records with no
        last_heartbeat fall back to registered_at."""
        store = self._registry_store
        if store is None:
            return
        records = await store.list(include_deregistered=False)
        for rec in records:
            seen = rec.last_heartbeat or rec.registered_at
            if seen is None:
                continue
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=timezone.utc)
            if (now - seen).total_seconds() > self._stale_after_s:
                try:
                    await store.mark_deregistered(rec.neuron_id)
                    logger.info(
                        "Dendrite: marked stale neuron %s deregistered "
                        "(last seen %s)", rec.neuron_id, seen.isoformat(),
                    )
                    await self._fire_refresh(RefreshEvent(
                        reason="stale", neuron_id=rec.neuron_id,
                    ))
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Dendrite: mark_deregistered(%s) failed in sweep: "
                        "%s", rec.neuron_id, exc,
                    )

    async def _dispatch_inbound(self, signal: Signal) -> None:
        if signal.type == SignalType.DISCOVER:
            await self._handle_discover(signal)
            return

        # Engram I/O requests: route RECALL/IMPRINT to a hosted Engram if
        # any matches (server side), then fire observer handlers
        # (@on_recall_signal / @on_imprint_signal) and return.
        if signal.type is SignalType.RECALL:
            await self._on_recall(signal)
            await self._fire_handlers(signal)
            return
        if signal.type is SignalType.IMPRINT:
            await self._on_imprint(signal)
            await self._fire_handlers(signal)
            return

        # Effector servicing: route a TOOL_CALL addressed to a hosted
        # Effector (server side) and publish TOOL_RESULT. Unlike
        # RECALL/IMPRINT this does NOT consume the signal - TOOL_CALL is
        # a PATHWAY_TYPES member, so trace observers, op-Pathway
        # correlation, and @on_tool_call handlers below must still see it.
        if signal.type is SignalType.TOOL_CALL and self._effectors:
            await self._on_tool_call(signal)

        # Workflow control: a STOP cancels everything this Dendrite owns on
        # the trace. Deliver to trace observers first (so @pw.on(STOP) fires),
        # then quiesce and ack.
        if signal.type is SignalType.STOP:
            if signal.trace_id:
                pw = self._pathways.get(signal.trace_id)
                if pw is not None:
                    try:
                        await pw._deliver(signal)
                    except Exception as exc:  # noqa: BLE001
                        logger.exception(
                            "Dendrite: STOP pathway deliver failed: %s", exc,
                        )
            await self._on_stop(signal)
            await self._fire_handlers(signal)
            return

        # Per-operation (request/reply) correlation: deliver any Signal whose
        # parent_id matches an open op-Pathway. This resolves the awaiting
        # recall()/imprint() (RECALLED/IMPRINTED) without a bespoke Future
        # table - EngramClient is just the wrapper that opened the Pathway.
        # Op ids are unique envelope ids, so this never misroutes. Delivery
        # continues below so trace observers still see the Signal too.
        if signal.parent_id:
            op_pw = self._op_pathways.get(signal.parent_id)
            if op_pw is not None:
                try:
                    await op_pw._deliver(signal)
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "Dendrite: op-Pathway delivery failed for %s: %s",
                        signal.type.value, exc,
                    )

        # Cache discrete decisions by parent_id so a later await_decision can
        # still resolve when the answer beat it onto the bus (an in-process
        # synapse delivers the whole request->answer chain inside the
        # original publish).
        if (
            signal.type in (SignalType.CLARIFICATION_ANSWER,
                            SignalType.PERMISSION_DECISION)
            and signal.parent_id
        ):
            self._recent_decisions[signal.parent_id] = signal
            while len(self._recent_decisions) > 256:
                self._recent_decisions.pop(next(iter(self._recent_decisions)))

        # Trace terminal events cancel any in-flight op I/O on the same trace
        # so awaiters in Neurons / orchestrators wake up (EngramCancelled)
        # instead of hanging on a deadline.
        if signal.type in (SignalType.FINAL, SignalType.ERROR) and signal.trace_id:
            try:
                await self._cancel_op_pathways(signal.trace_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Dendrite: op-Pathway cancel failed for %s: %s",
                    signal.trace_id, exc,
                )
            # Saga commit point is success (FINAL) only: discard each hosted
            # Engram's journal so successful writes become permanent. On ERROR
            # the journal is *kept* so the caller can still stop_trace(
            # rollback=True) to compensate a failed workflow (a plain
            # stop_trace, or a successful retry's preemptive STOP, discards it).
            if signal.type is SignalType.FINAL:
                for engram in list(self._engrams.values()):
                    try:
                        await engram.commit(signal.trace_id)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "Dendrite: Engram %s commit on FINAL failed: %s",
                            engram.engram_id, exc,
                        )
            self._trace_tasks.pop(signal.trace_id, None)

        # TASK_AWARDED targeting one of our Axons: synthesise a TASK
        # and route through the existing Axon handler.
        if signal.type is SignalType.TASK_AWARDED:
            target = signal.directed.id if signal.directed else None
            if target and target in self._axons:
                synthetic = task_signal(
                    trace_id=signal.trace_id, parent_id=signal.id,
                    directed=Directed(id=target),
                    input=signal.payload.get("input", {}),
                    context_ref=signal.payload.get("context_ref"),
                    finalize=bool(signal.payload.get("finalize")),
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

        # Engram registration: a REGISTER carrying the engram flag (or a
        # directed.type matching a known engram kind) announces an Engram
        # participant, not a Neuron. Record it in the engram-registration
        # table and stop - it must not pollute the Neuron registry store or
        # fire on_register_signal Neuron handlers.
        if signal.type is SignalType.REGISTER and self._is_engram_register(signal):
            self._record_engram_registration(signal)
            return

        # Default bidder: a hosted Axon whose caps cover the offer answers
        # automatically  -  unless the developer registered their own
        # on_task_offer handler (custom bidding logic wins outright) or
        # auto_bid=False.
        if (
            signal.type is SignalType.TASK_OFFER
            and self._auto_bid
            and self._axons
            and not self._handlers.get(SignalType.TASK_OFFER)
        ):
            await self._maybe_auto_bid(signal)

        if signal.type in AXON_TYPES and self._registry_store is not None:
            try:
                await self._update_registry(signal)
            except Exception as exc:
                logger.exception(
                    "Dendrite: registry update failed for %s: %s",
                    signal.type.value, exc,
                )
        await self._fire_handlers(signal)

    async def _fire_handlers(self, signal: Signal) -> None:
        """Fire every registered handler for ``signal.type``. Exceptions
        are logged, never propagated  -  one buggy handler must not break
        delivery to the others."""
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

    # ------------------------------------------------------------------
    # Engram registration (learned via REGISTER)
    # ------------------------------------------------------------------

    def _is_engram_register(self, signal: Signal) -> bool:
        """A REGISTER announces an Engram when its universal ``payload.role``
        is ``"engram"`` (or the legacy ``engram`` flag is set), or when its
        ``directed.type`` matches an engram kind this Dendrite already knows
        (hosted or previously registered)."""
        if signal.payload.get("role") == "engram" or signal.payload.get("engram"):
            return True
        d = signal.directed
        if d is not None and d.type:
            if d.type in self._engram_kind_index:
                return True
            if d.type in self._engram_reg_kind_index:
                return True
        return False

    def _record_engram_registration(self, signal: Signal) -> None:
        """Store an Engram registration learned from a REGISTER signal so
        future RECALL/IMPRINT addressed to its ``directed.id`` /
        ``directed.type`` are known to reach a participant on the Synapse."""
        d = signal.directed
        if d is None or (not d.id and not d.type):
            return
        caps = list(d.capabilities)
        if not caps:
            caps = list(signal.payload.get("capabilities", []) or [])
        directed = Directed(id=d.id, type=d.type, capabilities=caps)
        key = d.id or d.type
        assert key is not None
        self._engram_registrations[key] = directed
        if d.type:
            self._engram_reg_kind_index.setdefault(d.type, set()).add(key)

    @property
    def engram_registrations(self) -> dict[str, Directed]:
        """Engrams learned via REGISTER, keyed by directed.id (or
        directed.type when no id), including in-process ones."""
        return dict(self._engram_registrations)

    def is_engram_known(self, *, engram_id: str | None = None,
                        engram_kind: str | None = None) -> bool:
        """True when an Engram with this id/kind is reachable - hosted
        in-process or learned from a peer's REGISTER."""
        if engram_id:
            if engram_id in self._engrams or engram_id in self._engram_registrations:
                return True
        if engram_kind:
            if engram_kind in self._engram_kind_index or engram_kind in self._engram_reg_kind_index:
                return True
        return False

    async def _update_registry(self, signal: Signal) -> None:
        if self._registry_store is None:
            return
        neuron_id = signal.directed.id if signal.directed else None
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

        directed.id (engram_id) wins over directed.type (engram_kind). If
        neither matches a hosted Engram, returns []. If directed.type
        matches multiple hosted Engrams, every match is returned  -
        recall_mode handles the winner-selection on the caller side.
        """
        d = signal.directed
        eid = d.id if d else None
        if eid:
            ent = self._engrams.get(eid)
            return [ent] if ent is not None else []
        ekind = d.type if d else None
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
                # Attribute the reply to the Engram that answered, not the host
                # Dendrite, so observers (Prism) classify it by the Engram's own
                # REGISTER instead of inventing a node for the host id.
                directed=Directed(id=engram.engram_id, type=engram.engram_kind),
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
                    trace_id=signal.trace_id,
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
                    directed=Directed(id=engram.engram_id, type=engram.engram_kind),
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
                    directed=Directed(id=engram.engram_id, type=engram.engram_kind),
                )
            try:
                await self._publish(reply)
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "Dendrite: Engram %s IMPRINTED publish failed: %s",
                    engram.engram_id, exc,
                )

    # ------------------------------------------------------------------
    # Effector: hosted-side handlers
    # ------------------------------------------------------------------

    def _resolve_effector_targets(self, signal: Signal) -> list[Effector]:
        """Pick the hosted Effectors that should service a TOOL_CALL.

        directed.id (effector_id) wins over directed.type
        (effector_kind). If neither matches a hosted Effector, returns
        [] - the call may be served by another host, or consumed by a
        developer @on_tool_call handler (the legacy tool-server
        pattern), which still fires either way.
        """
        d = signal.directed
        eid = d.id if d else None
        if eid:
            ent = self._effectors.get(eid)
            return [ent] if ent is not None else []
        ekind = d.type if d else None
        if ekind:
            return [
                self._effectors[i]
                for i in self._effector_kind_index.get(ekind, [])
                if i in self._effectors
            ]
        return []

    async def _on_tool_call(self, signal: Signal) -> None:
        """Service a TOOL_CALL against hosted Effectors.

        Every failure mode (a can_serve miss aside) answers with a
        TOOL_RESULT carrying ``error`` rather than an ERROR signal, so a
        misbehaving tool never terminates the parent TASK. The reply is
        attributed to the Effector that answered (directed.id/type), not
        the host Dendrite, so observers (Prism) classify it correctly.
        """
        targets = self._resolve_effector_targets(signal)
        if not targets:
            return
        tool = signal.payload.get("tool", "")
        args = signal.payload.get("args") or {}
        call_id = signal.payload.get("call_id")
        for effector in targets:
            try:
                if not await effector.can_serve(tool):
                    continue
                outcome = await effector.invoke(
                    tool, args,
                    call_id=call_id,
                    deadline_ms=signal.payload.get("deadline_ms"),
                    trace_id=signal.trace_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "Dendrite: Effector %s.invoke raised: %s",
                    effector.effector_id, exc,
                )
                reply = tool_result_signal(
                    trace_id=signal.trace_id,
                    parent_id=signal.id,
                    tool=tool,
                    error=f"effector_exception: {exc}",
                    call_id=call_id,
                    directed=Directed(
                        id=effector.effector_id,
                        type=effector.effector_kind,
                    ),
                )
            else:
                reply = tool_result_signal(
                    trace_id=signal.trace_id,
                    parent_id=signal.id,
                    tool=outcome.tool or tool,
                    result=outcome.result,
                    error=outcome.error,
                    call_id=outcome.call_id or call_id,
                    directed=Directed(
                        id=effector.effector_id,
                        type=effector.effector_kind,
                    ),
                )
            try:
                await self._publish(reply)
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "Dendrite: Effector %s TOOL_RESULT publish failed: %s",
                    effector.effector_id, exc,
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

    @property
    def effector_client(self) -> EffectorClient:
        """Caller-side tool correlation table - the action-side twin of
        ``engram_client``, surfaced for the Axon to call directly."""
        return self._effector_client

    @staticmethod
    def _resolve_trace(
        trace_id: str | None, parent_id: str | None
    ) -> "tuple[str, str]":
        """Resolve (trace_id, parent_id) for a caller-side engram op.

        Explicit ids always win. With no explicit trace_id, the ambient
        task context (bound by ``Axon.handle_task``) is inherited, so ops
        fired from detector / lifecycle hooks land on the containing
        TASK's trace per ENGRAM_DESIGN.md §5.4. Only outside any task
        (e.g. pre-task hydration) is a fresh trace minted. An explicitly
        supplied trace_id never mixes with the ambient parent_id.
        """
        from cosmonapse.envelope import ambient_trace, new_event_id

        tid = trace_id
        pid = parent_id
        if tid is None:
            amb = ambient_trace()
            if amb is not None:
                tid = amb[0]
                if pid is None:
                    pid = amb[1]
        if tid is None:
            tid = new_trace_id()
        if pid is None:
            # Synthesise a root parent_id so the envelope validates.
            pid = new_event_id()
        return tid, pid

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

        Trace attribution follows ``_resolve_trace``: explicit ids win,
        then the ambient task context, then a freshly minted trace (use
        that shape for pre-task hydration).
        """
        tid, pid = self._resolve_trace(trace_id, parent_id)
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

    async def call_tool(
        self,
        *,
        effector_id: str | None = None,
        effector_kind: str | None = None,
        tool: str,
        args: dict[str, Any] | None = None,
        call_id: str | None = None,
        deadline_ms: int | None = None,
        trace_id: str | None = None,
        parent_id: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> ToolOutcome:
        """Emit TOOL_CALL and await TOOL_RESULT.

        Trace attribution follows ``_resolve_trace``: explicit ids win,
        then the ambient task context, then a freshly minted trace.
        """
        tid, pid = self._resolve_trace(trace_id, parent_id)
        return await self._effector_client.call(
            effector_id=effector_id,
            effector_kind=effector_kind,
            tool=tool,
            args=args,
            call_id=call_id,
            deadline_ms=deadline_ms,
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
        """Emit IMPRINT. Returns None unless ``await_ack=True``.

        Trace attribution follows ``_resolve_trace``: explicit ids win,
        then the ambient task context, then a freshly minted trace - so an
        imprint fired from a ``@detects_output`` hook is attributed to the
        TASK it concluded.
        """
        tid, pid = self._resolve_trace(trace_id, parent_id)
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
