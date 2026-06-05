"""
cosmonapse.synapse.kafka
~~~~~~~~~~~~~~~~~~~~~~~~~~
Kafka Synapse adapter.

Kafka does not map onto the Cosmonapse Synapse contract as cleanly as
NATS  -  a few translations are needed:

  Cosmonapse                       Kafka
  ----------------------------     ----------------------------------
  subject `a.b.TYPE`               topic `a.b.TYPE` (verbatim)
  wildcard `a.b.*` or `a.>`        regex consumer subscription
                                   (`a\\.b\\.[^.]+`  /  `a\\..+`)
  queue_group="workers"            consumer `group_id="workers"`
  no queue_group (Doppler)         consumer `group_id=<unique>`
                                   so it joins its own group and sees
                                   every message (Doppler behaviour)
  request / reply                  per-call reply topic; the requester
                                   subscribes to its inbox before
                                   publishing, then awaits one message
                                   matching the request's signal id.

The `aiokafka` library is **lazy-imported**  -  the module imports fine
without it; `connect()` raises a clear `ImportError` if it is missing.

Install:
    pip install "cosmonapse[kafka]"   # or: pip install aiokafka

Caveats
-------
- Kafka topics must exist (auto.create.topics.enable=true on the broker
  if you want them created on first publish).
- `bid_window` / fan-out style request-reply is poorly suited to Kafka.
  Prefer NATS for high-fan-out routing; use Kafka where you want the
  long-term audit log of every Signal that crossed the Synapse.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from typing import TYPE_CHECKING, Any

from cosmonapse.envelope import Signal
from cosmonapse.synapse.base import MessageHandler, Subscription, Synapse

if TYPE_CHECKING:
    from aiokafka import AIOKafkaConsumer, AIOKafkaProducer  # noqa: F401  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)


def _subject_to_topic_regex(pattern: str) -> str | None:
    """
    Convert a Cosmonapse subject (with optional `*` / `>` wildcards)
    into a Kafka topic regex string. Returns None if the subject is
    not a wildcard pattern (caller should use exact-topic subscribe).
    """
    if "*" not in pattern and ">" not in pattern:
        return None
    parts: list[str] = []
    for tok in pattern.split("."):
        if tok == "*":
            parts.append(r"[^.]+")
        elif tok == ">":
            parts.append(r".+")
        else:
            parts.append(re.escape(tok))
    return "^" + r"\.".join(parts) + "$"


class _KafkaSubscription(Subscription):
    def __init__(self, synapse: "KafkaSynapse", consumer_task: asyncio.Task[None],
                 consumer: Any) -> None:
        self._synapse = synapse
        self._task = consumer_task
        self._consumer = consumer
        self._active = True

    async def unsubscribe(self) -> None:
        if not self._active:
            return
        self._active = False
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        try:
            await self._consumer.stop()
        except Exception:
            pass


class KafkaSynapse(Synapse):
    """
    Kafka-backed Synapse.

    Parameters
    ----------
    bootstrap_servers   Kafka broker(s), e.g. "localhost:9092" or list.
    client_id           Optional client identifier.
    producer_options    Extra kwargs forwarded to AIOKafkaProducer.
    consumer_options    Extra kwargs forwarded to AIOKafkaConsumer.
    """

    def __init__(
        self,
        *,
        bootstrap_servers: str | list[str] = "localhost:9092",
        client_id: str | None = None,
        producer_options: dict[str, Any] | None = None,
        consumer_options: dict[str, Any] | None = None,
    ) -> None:
        self._bootstrap = bootstrap_servers
        self._client_id = client_id
        self._producer_options = producer_options or {}
        self._consumer_options = consumer_options or {}
        self._producer: Any = None
        self._consumers: list[Any] = []
        self._connected = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        if self._connected:
            return
        try:
            from aiokafka import AIOKafkaProducer
        except ImportError as exc:
            raise ImportError(
                "KafkaSynapse requires the 'aiokafka' package. "
                "Install it with: pip install 'cosmonapse[kafka]'  "
                "(or: pip install aiokafka)"
            ) from exc

        servers = (self._bootstrap if isinstance(self._bootstrap, str)
                   else ",".join(self._bootstrap))
        self._producer = AIOKafkaProducer(
            bootstrap_servers=servers,
            client_id=self._client_id,
            **self._producer_options,
        )
        await self._producer.start()
        self._connected = True
        logger.info("KafkaSynapse connected to %s", servers)

    async def close(self) -> None:
        if not self._connected:
            return
        for c in self._consumers:
            try:
                await c.stop()
            except Exception:
                pass
        self._consumers.clear()
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None
        self._connected = False
        logger.info("KafkaSynapse closed")

    # ------------------------------------------------------------------
    # Publish
    # ------------------------------------------------------------------

    async def publish(self, subject: str, signal: Signal) -> None:
        if not self._connected or self._producer is None:
            raise RuntimeError("KafkaSynapse.publish called before connect()")
        await self._producer.send_and_wait(subject, signal.encode())

    # ------------------------------------------------------------------
    # Subscribe
    # ------------------------------------------------------------------

    async def subscribe(
        self,
        subject: str,
        handler: MessageHandler,
        *,
        queue_group: str | None = None,
    ) -> Subscription:
        if not self._connected:
            raise RuntimeError("KafkaSynapse.subscribe called before connect()")

        from aiokafka import AIOKafkaConsumer

        servers = (self._bootstrap if isinstance(self._bootstrap, str)
                   else ",".join(self._bootstrap))

        # No queue_group => the Doppler pattern; each such consumer gets
        # a unique group_id so it joins its own group and sees every record.
        group_id = queue_group if queue_group else f"cosmonapse-solo-{uuid.uuid4().hex[:12]}"

        consumer = AIOKafkaConsumer(
            bootstrap_servers=servers,
            group_id=group_id,
            client_id=self._client_id,
            enable_auto_commit=True,
            auto_offset_reset="latest",
            **self._consumer_options,
        )

        topic_regex = _subject_to_topic_regex(subject)
        await consumer.start()
        if topic_regex is not None:
            consumer.subscribe(pattern=topic_regex)
        else:
            consumer.subscribe(topics=[subject])

        async def _pump() -> None:
            try:
                async for msg in consumer:
                    try:
                        signal = Signal.decode(msg.value)
                    except Exception as exc:
                        logger.warning(
                            "KafkaSynapse: decode failed on %s: %s",
                            msg.topic, exc,
                        )
                        continue
                    try:
                        await handler(signal)
                    except Exception as exc:
                        logger.exception(
                            "KafkaSynapse: handler raised on %s: %s",
                            msg.topic, exc,
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("KafkaSynapse: pump loop crashed: %s", exc)

        task = asyncio.create_task(_pump())
        self._consumers.append(consumer)
        return _KafkaSubscription(self, task, consumer)

    # ------------------------------------------------------------------
    # Request / reply
    # ------------------------------------------------------------------

    async def request(
        self,
        subject: str,
        signal: Signal,
        *,
        timeout_s: float = 5.0,
    ) -> Signal:
        """
        Per-call reply topic. The requester subscribes to a private
        topic, embeds it in `meta._reply_to`, publishes the request,
        and awaits the first message whose `parent_id` matches the
        request's signal id.

        Note: this is a polyfill, not a deep Kafka idiom. For heavy
        request/reply workloads prefer NATS.
        """
        if not self._connected:
            raise RuntimeError("KafkaSynapse.request called before connect()")

        reply_topic = f"_inbox.{uuid.uuid4().hex}"
        fut: asyncio.Future[Signal] = asyncio.get_running_loop().create_future()

        async def _on_reply(reply: Signal) -> None:
            if fut.done():
                return
            if reply.parent_id == signal.id:
                fut.set_result(reply)

        sub = await self.subscribe(reply_topic, _on_reply)

        enriched = signal.model_copy(
            update={"meta": {**signal.meta, "_reply_to": reply_topic}}
        )

        try:
            await self.publish(subject, enriched)
            return await asyncio.wait_for(fut, timeout=timeout_s)
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"KafkaSynapse: no reply on {reply_topic!r} within {timeout_s}s"
            )
        finally:
            await sub.unsubscribe()
