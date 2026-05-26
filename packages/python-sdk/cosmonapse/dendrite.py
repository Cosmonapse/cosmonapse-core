"""
cosmonapse.dendrite
~~~~~~~~~~~~~~~~~~~
The Dendrite is the synapse-side participant.

Construction is minimal: only `synapse` is required. Every other
behaviour is opt-in:

  - Attach Axons -> Dendrite subscribes to TASK, emits REGISTER /
    HEARTBEAT / DEREGISTER, routes inbound TASKs to the right Axon.
  - Register a handler (e.g. @dendrite.on_agent_output) -> Dendrite
    subscribes to that AXON_TYPE on the namespace and dispatches.
  - Pass a registry_store -> Dendrite mirrors its own attached Axons
    into it AND (if any inbound-handler subscription is wired) updates
    the store from inbound REGISTER / DEREGISTER / HEARTBEAT signals
    seen on the bus.
  - heartbeat_s = 0 -> the heartbeat loop never starts.

The Dendrite does NOT own the Synapse — the caller builds it (e.g.
`await connect_synapse("cosmo://...")`) and closes it.

Orchestration
-------------
There is no separate Cortex class. Every Dendrite has `dispatch_task`,
`emit_final`, `emit_error`, `emit`, and the inbound-handler decorators.
A "Cortex" is just a Dendrite that uses them. (The `Cortex` symbol is
kept as a back-compat alias.)
"""

from __future__ import annotations

import asyncio
import logging
import warnings
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from cosmonapse._hooks import LifecycleHooks, RefreshEvent
from cosmonapse.axon import Axon
from cosmonapse.envelope import (
    AXON_TYPES,
    SYNAPSE_TYPES,
    Signal,
    SignalType,
    deregister_signal,
    error_signal,
    final_signal,
    heartbeat_signal,
    register_signal,
    task_signal,
)
from cosmonapse.storage.base import NeuronRecord, RegistryStore
from cosmonapse.synapse.base import MessageHandler, Subscription, Synapse

logger = logging.getLogger(__name__)


SignalHandler = Callable[[Signal], Awaitable[None]]


class DendriteProtocolError(ValueError):
    """Raised when an emit violates the protocol (e.g. emitting an AXON-only type)."""


# Back-compat aliases.
CortexProtocolError = DendriteProtocolError


class Dendrite(LifecycleHooks):
    """
    Synapse-side participant. Synapse required, everything else optional.

    Parameters
    ----------
    synapse           Connected Synapse. Required. Caller closes it.
    registry_store    Optional RegistryStore. None disables registry mirror.
    namespace         Synapse namespace. Default "default".
    dendrite_id       Identifier embedded in this Dendrite's outbound
                      FINAL / ERROR signals as `neuron` (cosmetic).
    heartbeat_s             Per-attached-Axon heartbeat interval. Pass 0 to
                            disable the heartbeat loop entirely.
    reregister_on_heartbeat Re-emit REGISTER on every heartbeat tick so
                            late-joining consumers discover the axon without
                            a dedicated sync. Default True. Set to False to
                            emit REGISTER only once at startup.
    """

    def __init__(
        self,
        *,
        synapse: Synapse,
        registry_store: RegistryStore | None = None,
        namespace: str = "default",
        dendrite_id: str = "dendrite",
        heartbeat_s: float = 30.0,
        reregister_on_heartbeat: bool = True,
    ) -> None:
        if synapse is None:
            raise TypeError("Dendrite requires a synapse (Synapse)")
        LifecycleHooks.__init__(self)

        self._synapse = synapse
        self._registry_store = registry_store
        self._namespace = namespace
        self.dendrite_id = dendrite_id
        self._heartbeat_s = heartbeat_s
        self._reregister_on_heartbeat = reregister_on_heartbeat

        self._axons: dict[str, Axon] = {}
        self._handlers: dict[SignalType, list[SignalHandler]] = {
            t: [] for t in AXON_TYPES
        }

        self._task_sub: Subscription | None = None
        self._inbound_subs: dict[SignalType, Subscription] = {}
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._running = False

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

    # ------------------------------------------------------------------
    # Attachment
    # ------------------------------------------------------------------

    def attach_axon(self, axon: Axon) -> None:
        """Attach an Axon. Once running, the Dendrite will route TASKs
        addressed to this Axon and emit REGISTER / HEARTBEAT / DEREGISTER."""
        if axon.neuron_id in self._axons:
            raise ValueError(
                f"Dendrite already has an Axon for neuron_id={axon.neuron_id!r}"
            )
        self._axons[axon.neuron_id] = axon
        axon.attach_to(self)

    # ------------------------------------------------------------------
    # Handler registration (decorators) — wires inbound subs lazily
    # ------------------------------------------------------------------

    def _on(self, signal_type: SignalType) -> Callable[[SignalHandler], SignalHandler]:
        def decorator(fn: SignalHandler) -> SignalHandler:
            self._handlers[signal_type].append(fn)
            # If the Dendrite is already running, wire the subscription
            # for this type now. Otherwise start() will pick it up.
            if self._running and signal_type not in self._inbound_subs:
                asyncio.create_task(self._ensure_inbound_sub(signal_type))
            return fn
        return decorator

    # Canonical handler decorators. AGENT_OUTPUT and CLARIFICATION have no
    # ``_signal`` suffix because they never collided with another name; the
    # management-signal decorators use the explicit ``_signal`` suffix.
    def on_agent_output(self, fn): return self._on(SignalType.AGENT_OUTPUT)(fn)
    def on_clarification(self, fn): return self._on(SignalType.CLARIFICATION)(fn)
    def on_error_signal(self, fn): return self._on(SignalType.ERROR)(fn)
    def on_register_signal(self, fn): return self._on(SignalType.REGISTER)(fn)
    def on_deregister_signal(self, fn): return self._on(SignalType.DEREGISTER)(fn)
    def on_heartbeat_signal(self, fn): return self._on(SignalType.HEARTBEAT)(fn)

    # ------------------------------------------------------------------
    # Deprecated short aliases. The ``_signal`` forms above are canonical;
    # these will be removed in a future release.
    # ------------------------------------------------------------------
    def _deprecated_alias(self, old: str, new: str) -> None:
        warnings.warn(
            f"Dendrite.{old} is deprecated and will be removed in a future "
            f"release; use Dendrite.{new} instead.",
            DeprecationWarning,
            stacklevel=3,
        )

    def on_error(self, fn):
        self._deprecated_alias("on_error", "on_error_signal")
        return self.on_error_signal(fn)

    def on_register(self, fn):
        self._deprecated_alias("on_register", "on_register_signal")
        return self.on_register_signal(fn)

    def on_deregister(self, fn):
        self._deprecated_alias("on_deregister", "on_deregister_signal")
        return self.on_deregister_signal(fn)

    def on_heartbeat(self, fn):
        self._deprecated_alias("on_heartbeat", "on_heartbeat_signal")
        return self.on_heartbeat_signal(fn)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return

        if self._registry_store is not None:
            await self._registry_store.connect()

        # Only subscribe to TASK if there's an Axon to route to.
        if self._axons:
            self._task_sub = await self._synapse.subscribe(
                self._subject(SignalType.TASK),
                self._on_task,
            )
            for axon in self._axons.values():
                await self._mirror_to_store(axon, status="registered")
                await self._emit_register(axon)
                await axon._on_register_emitted()

        # Wire inbound subs only for types with registered handlers.
        for signal_type, handlers in self._handlers.items():
            if handlers:
                await self._ensure_inbound_sub(signal_type)

        # If a registry_store is configured, also auto-wire subs for the
        # three management types so the store tracks the namespace-wide
        # view (REGISTER from other peers, etc.). Without a store you
        # observe nothing automatically.
        if self._registry_store is not None:
            for mgmt_type in (SignalType.REGISTER, SignalType.DEREGISTER,
                              SignalType.HEARTBEAT):
                await self._ensure_inbound_sub(mgmt_type)

        self._running = True

        # Heartbeat loop only if we have axons to heartbeat for and it's enabled.
        if self._axons and self._heartbeat_s > 0:
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        self._launch_schedule()
        await self._fire_connect()

        logger.info(
            "Dendrite %s started on namespace %r (axons=%d, inbound_subs=%d)",
            self.dendrite_id, self._namespace,
            len(self._axons), len(self._inbound_subs),
        )

    async def stop(self, reason: str | None = None) -> None:
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

        for sub in list(self._inbound_subs.values()):
            try:
                await sub.unsubscribe()
            except Exception as exc:
                logger.warning("Dendrite failed to unsubscribe inbound: %s", exc)
        self._inbound_subs.clear()

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

        # NOTE: the Dendrite does NOT own the Synapse. The caller closes it.

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
                "Dendrite has no registry_store — pass one at construction "
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
        self, *, neuron: str, input: dict[str, Any],
        trace_id: str | None = None, parent_id: str | None = None,
        context_ref: str | None = None,
        capabilities: list[str] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Signal:
        sig = task_signal(
            trace_id=trace_id, parent_id=parent_id, neuron=neuron,
            input=input, context_ref=context_ref,
            capabilities=capabilities, meta=meta,
        )
        await self.emit(sig)
        return sig

    async def emit_final(self, *, trace_id, parent_id, result, meta=None) -> Signal:
        sig = final_signal(trace_id=trace_id, parent_id=parent_id,
                           neuron=self.dendrite_id, result=result, meta=meta)
        await self.emit(sig)
        return sig

    async def emit_error(self, *, trace_id, parent_id, code, message,
                         recoverable=False, meta=None) -> Signal:
        sig = error_signal(trace_id=trace_id, parent_id=parent_id,
                           neuron=self.dendrite_id, code=code, message=message,
                           recoverable=recoverable, meta=meta)
        await self.emit(sig)
        return sig

    async def emit(self, signal: Signal) -> None:
        if signal.type not in SYNAPSE_TYPES:
            raise DendriteProtocolError(
                f"Dendrite refuses to emit {signal.type.value!r}: "
                f"only synapse-side types (SYNAPSE_TYPES) may be emitted "
                f"this way. {signal.type.value} is an Axon-owned type."
            )
        await self._publish(signal)

    async def _publish(self, signal: Signal) -> None:
        """Internal, unchecked publish.

        Bypasses the ``emit()`` SYNAPSE_TYPES guard so the Dendrite itself can
        relay Axon-owned signals (the REGISTER / HEARTBEAT / DEREGISTER it
        emits on behalf of its Axons, and the AGENT_OUTPUT / CLARIFICATION /
        ERROR replies an Axon produces). This is deliberately private — public
        code must go through ``emit()`` so the protocol guard cannot be
        accidentally circumvented.
        """
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

    async def _ensure_inbound_sub(self, signal_type: SignalType) -> None:
        if signal_type in self._inbound_subs:
            return
        sub = await self.subscribe(signal_type, self._dispatch_inbound)
        self._inbound_subs[signal_type] = sub

    async def _on_task(self, task: Signal) -> None:
        target = task.neuron
        if not target:
            return
        axon = self._axons.get(target)
        if axon is None:
            return

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

    async def _emit_register(self, axon: Axon) -> None:
        await self._publish(register_signal(
            neuron=axon.neuron_id,
            capabilities=axon.capabilities,
            version=axon.version,
        ))

    async def _emit_deregister(self, axon: Axon, *, reason: str | None) -> None:
        await self._publish(deregister_signal(neuron=axon.neuron_id, reason=reason))

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
                    # Re-emit REGISTER alongside HEARTBEAT so late-joining
                    # consumers catch up without a separate sync mechanism.
                    # Can be disabled via reregister_on_heartbeat=False.
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
        if signal.type not in AXON_TYPES:
            return
        if self._registry_store is not None:
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


# Back-compat alias — Cortex is just a Dendrite.
Cortex = Dendrite
