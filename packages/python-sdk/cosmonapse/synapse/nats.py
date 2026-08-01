"""
cosmonapse.synapse.nats
~~~~~~~~~~~~~~~~~~~~~~~~~
NATS Synapse adapter.

NATS maps onto the Cosmonapse Synapse contract very directly:

  - Subjects use the same `cosmonapse.<namespace>.<TYPE>` convention.
  - `*` and `>` wildcards are native NATS  -  no translation needed.
  - Queue groups are native (`queue=...` on subscribe).
  - Request / reply is native (`nc.request`).

The `nats-py` library is **lazy-imported** so this module is safe to
load even when nats-py is not installed. The import only happens
inside `connect()`; if the package is missing, a clear `ImportError`
is raised pointing at the right `pip install`.

Install:
    pip install cosmonapse   # or: pip install nats-py
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from cosmonapse.envelope import Signal
from cosmonapse.synapse.base import MessageHandler, Subscription, Synapse

if TYPE_CHECKING:
    from nats.aio.client import Client as NATSClient
    from nats.aio.subscription import Subscription as NATSSubscription

logger = logging.getLogger(__name__)


class _NatsSubscription(Subscription):
    def __init__(self, nats_sub: NATSSubscription) -> None:
        self._nats_sub = nats_sub
        self._active = True

    async def unsubscribe(self) -> None:
        if self._active:
            await self._nats_sub.unsubscribe()
            self._active = False


class NatsSynapse(Synapse):
    """
    NATS-backed Synapse. Pass a NATS URL (or a list of URLs) plus any
    extra `nats.aio.client.Client.connect(**options)` arguments via
    `connect_options`.

    Example:
        synapse = NatsSynapse(url="nats://localhost:4222")
        await synapse.connect()
        dendrite = Dendrite(synapse=synapse, namespace="prod")

    Notes
    -----
    - `Signal` is serialised with `Signal.encode()` (UTF-8 JSON bytes).
    - `subscribe(..., queue_group="x")` translates to NATS queue groups,
      so many subscribers in the same group form a load-balanced
      worker pool exactly like the in-memory adapter.
    - `request()` uses `nc.request` under the hood, which is a NATS
      inbox-based RPC. Reply timeout is enforced server-side.
    """

    def __init__(
        self,
        *,
        url: str | list[str] = "nats://127.0.0.1:4222",
        connect_options: dict[str, Any] | None = None,
    ) -> None:
        self._url = url
        self._connect_options: dict[str, Any] = connect_options or {}
        self._nc: NATSClient | None = None
        self._connected = False

    async def connect(self) -> None:
        if self._connected:
            return

        try:
            from nats.aio.client import Client as NATSClient
        except ImportError as exc:
            raise ImportError(
                "NatsSynapse requires the 'nats-py' package. "
                "Install it with: pip install cosmonapse  "
                "(or: pip install nats-py)"
            ) from exc

        nc = NATSClient()
        servers = self._url if isinstance(self._url, list) else [self._url]
        await nc.connect(servers=servers, **self._connect_options)
        self._nc = nc
        self._connected = True
        logger.info("NatsSynapse connected to %s", servers)

    async def close(self) -> None:
        if not self._connected or self._nc is None:
            return
        await self._nc.drain()
        await self._nc.close()
        self._nc = None
        self._connected = False
        logger.info("NatsSynapse closed")

    async def publish(self, subject: str, signal: Signal) -> None:
        if not self._connected or self._nc is None:
            raise RuntimeError("NatsSynapse.publish called before connect()")
        await self._nc.publish(subject, signal.encode())

    async def subscribe(
        self,
        subject: str,
        handler: MessageHandler,
        *,
        queue_group: str | None = None,
    ) -> Subscription:
        if not self._connected or self._nc is None:
            raise RuntimeError("NatsSynapse.subscribe called before connect()")

        async def _bridge(msg: Any) -> None:
            try:
                signal = Signal.decode(msg.data)
            except Exception as exc:
                logger.warning(
                    "NatsSynapse: failed to decode signal on %s: %s",
                    subject, exc,
                )
                return
            try:
                await handler(signal)
            except Exception as exc:
                logger.exception(
                    "NatsSynapse: handler for %s raised: %s", subject, exc,
                )

        sub = await self._nc.subscribe(subject, queue=queue_group, cb=_bridge)  # type: ignore[arg-type]
        return _NatsSubscription(sub)

    async def request(
        self,
        subject: str,
        signal: Signal,
        *,
        timeout_s: float = 5.0,
    ) -> Signal:
        if not self._connected or self._nc is None:
            raise RuntimeError("NatsSynapse.request called before connect()")
        try:
            msg = await self._nc.request(
                subject, signal.encode(), timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"NatsSynapse: no reply on {subject!r} within {timeout_s}s"
            )
        return Signal.decode(msg.data)
