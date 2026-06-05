"""
cosmonapse._hooks
~~~~~~~~~~~~~~~~~
Shared lifecycle-hook surface for Axon, Dendrite, Cortex.

Three hook kinds  -  chosen because they cover both the centralised
(orchestrator-first) and decentralised (peer-to-peer) cases:

    on_connect       fire-once after this component finishes its own
                     connect handshake (Axon attached to a Dendrite,
                     Dendrite up on the Synapse, Cortex orchestration
                     wired)

    on_refresh       fired internally whenever the component's
                     observable state refreshes  -  heartbeat tick,
                     REGISTER / DEREGISTER / HEARTBEAT seen by the
                     Cortex's registry, a manual `await refresh()`.
                     The handler receives a RefreshEvent describing
                     what changed.

    on_schedule      developer-supplied periodic task. Runs as a
                     background coroutine every `every_s` seconds
                     until the component stops.

Decentralised use case
----------------------
Without a central Cortex, each Dendrite can use:

    @dendrite.on_connect
    async def announce(d):
        # peer hello  -  broadcast our local registry to anyone listening
        ...

    @dendrite.on_refresh
    async def reconcile(d, event):
        # re-emit REGISTER, prune stale peers, etc.
        ...

    @dendrite.on_schedule(every_s=30)
    async def gossip(d):
        # exchange state with a random peer
        ...

This is the surface that lets peer-to-peer fabrics emerge without the
SDK baking in an orchestration model.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Refresh event
# ---------------------------------------------------------------------------

@dataclass
class RefreshEvent:
    """Context passed to on_refresh hooks.

    reason     "heartbeat" | "register" | "deregister" | "manual" |
               "scheduled"
    neuron_id  the neuron implicated in the change, if any
    extra      free-form bag for component-specific detail
    """
    reason: str
    neuron_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Hook function shapes
# ---------------------------------------------------------------------------

# on_connect / on_schedule:  fn(owner) -> None | Awaitable[None]
# on_refresh:                fn(owner, event: RefreshEvent) -> None | Awaitable[None]
ConnectHook = Callable[[Any], Awaitable[None] | None]
RefreshHook = Callable[[Any, RefreshEvent], Awaitable[None] | None]
ScheduleHook = Callable[[Any], Awaitable[None] | None]


# ---------------------------------------------------------------------------
# The mixin
# ---------------------------------------------------------------------------

class LifecycleHooks:
    """
    Mixin that grafts on_connect / on_refresh / on_schedule onto a
    component. The component is responsible for calling:

      - await self._fire_connect()      once start() has wired everything
      - self._launch_schedule()         to start the on_schedule loops
      - await self._fire_refresh(event) whenever state observably changes
      - await self._stop_hooks()        in stop(), to cancel background tasks
    """

    def __init__(self) -> None:
        self._connect_hooks: list[ConnectHook] = []
        self._refresh_hooks: list[RefreshHook] = []
        self._schedule_hooks: list[tuple[float, ScheduleHook]] = []
        self._scheduled_tasks: list[asyncio.Task[None]] = []
        self._hooks_started = False

    # -- decorators ----------------------------------------------------

    def on_connect(self, fn: ConnectHook) -> ConnectHook:
        """Register a fire-once handler called after start() completes."""
        self._connect_hooks.append(fn)
        return fn

    def on_refresh(self, fn: RefreshHook) -> RefreshHook:
        """Register a handler called whenever this component's state refreshes."""
        self._refresh_hooks.append(fn)
        return fn

    def on_schedule(self, *, every_s: float) -> Any:
        """
        Register a periodic handler. Background task runs every
        `every_s` seconds for the lifetime of the component.

        Usage:
            @dendrite.on_schedule(every_s=10)
            async def heartbeat_metric(d): ...
        """
        if every_s <= 0:
            raise ValueError("on_schedule requires every_s > 0")

        def decorator(fn: ScheduleHook) -> ScheduleHook:
            self._schedule_hooks.append((every_s, fn))
            # If the component is already running, start this loop now too.
            if self._hooks_started:
                self._scheduled_tasks.append(
                    asyncio.create_task(self._schedule_loop(every_s, fn))
                )
            return fn
        return decorator

    # -- driven by the host component ---------------------------------

    async def _fire_connect(self) -> None:
        for h in list(self._connect_hooks):
            try:
                await _call(h, self)
            except Exception as exc:
                logger.exception(
                    "%s on_connect hook raised: %s",
                    type(self).__name__, exc,
                )

    async def _fire_refresh(self, event: RefreshEvent) -> None:
        for h in list(self._refresh_hooks):
            try:
                await _call_with_event(h, self, event)
            except Exception as exc:
                logger.exception(
                    "%s on_refresh hook raised: %s",
                    type(self).__name__, exc,
                )

    def _launch_schedule(self) -> None:
        if self._hooks_started:
            return
        for interval, fn in self._schedule_hooks:
            self._scheduled_tasks.append(
                asyncio.create_task(self._schedule_loop(interval, fn))
            )
        self._hooks_started = True

    async def _stop_hooks(self) -> None:
        for t in self._scheduled_tasks:
            t.cancel()
        for t in self._scheduled_tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        self._scheduled_tasks.clear()
        self._hooks_started = False

    # -- driver for an individual schedule loop -----------------------

    async def _schedule_loop(self, interval: float, fn: ScheduleHook) -> None:
        try:
            while True:
                try:
                    await asyncio.sleep(interval)
                except asyncio.CancelledError:
                    return
                try:
                    await _call(fn, self)
                except Exception as exc:
                    logger.exception(
                        "%s scheduled hook raised: %s",
                        type(self).__name__, exc,
                    )
        except asyncio.CancelledError:
            return

    # -- public manual trigger ----------------------------------------

    async def refresh(self, *, reason: str = "manual",
                      neuron_id: str | None = None,
                      extra: dict[str, Any] | None = None) -> None:
        """
        Manually fire a refresh event. Useful when a developer's own
        code knows that internal state has changed.
        """
        await self._fire_refresh(RefreshEvent(
            reason=reason, neuron_id=neuron_id, extra=extra or {},
        ))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _call(fn: ConnectHook, owner: Any) -> None:
    result = fn(owner)
    if asyncio.iscoroutine(result):
        await result


async def _call_with_event(fn: RefreshHook, owner: Any,
                           event: RefreshEvent) -> None:
    result = fn(owner, event)
    if asyncio.iscoroutine(result):
        await result
