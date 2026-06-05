"""
cosmonapse.synapse.memory
~~~~~~~~~~~~~~~~~~~~~~~~~~~
In-memory synapse adapter.

This is NOT a throwaway test double. It is the adapter that `cosmo dev`
runs under the hood and what makes the "first five minutes" experience
work with zero external infrastructure.

It implements the full Synapse interface so any code written against
MemorySynapse works without modification against NatsSynapse.

Wildcard matching supports two forms (same as NATS):
  *   matches exactly one token     cosmonapse.team_a.*
  >   matches one or more tokens    cosmonapse.>
"""

from __future__ import annotations

import asyncio
import fnmatch
from collections import defaultdict
from typing import Any

from cosmonapse.envelope import Signal
from cosmonapse.synapse.base import MessageHandler, Subscription, Synapse


class MemorySubscription(Subscription):
    def __init__(self, synapse: "MemorySynapse", subject: str, handler_id: int) -> None:
        self._synapse = synapse
        self._subject = subject
        self._handler_id = handler_id
        self._active = True

    async def unsubscribe(self) -> None:
        if self._active:
            self._synapse._remove_handler(self._subject, self._handler_id)
            self._active = False


class MemorySynapse(Synapse):
    """
    Async in-memory synapse backed by asyncio queues.

    Supports:
    - Fan-out: multiple subscribers on the same subject each receive the message
    - Queue groups: within a group, only one subscriber receives each message
                    (round-robin delivery)
    - Wildcards: * (one token) and > (one or more tokens)
    - Request/reply: publish with a reply-to subject, wait for one response

    Thread safety: designed for single-event-loop use (asyncio). Not thread-safe.
    """

    def __init__(self) -> None:
        # subject → list of (handler_id, queue_group | None, handler)
        self._subs: dict[str, list[tuple[int, str | None, MessageHandler]]] = defaultdict(list)
        self._counter: int = 0
        self._connected: bool = False
        # Per-group round-robin counters for deterministic load balancing
        self._rr_counters: dict[str, int] = defaultdict(int)

    async def connect(self) -> None:
        self._connected = True

    async def close(self) -> None:
        self._subs.clear()
        self._connected = False

    def _next_id(self) -> int:
        self._counter += 1
        return self._counter

    def _remove_handler(self, subject: str, handler_id: int) -> None:
        self._subs[subject] = [
            entry for entry in self._subs[subject] if entry[0] != handler_id
        ]

    @staticmethod
    def _matches(pattern: str, subject: str) -> bool:
        """
        Return True if subject matches pattern.

        Pattern tokens:
          *  matches any single token (no dots)
          >  matches any sequence of tokens (must be the last token)
        """
        if pattern == subject:
            return True

        # Convert to fnmatch-compatible pattern
        # NATS:   cosmonapse.*.TASK  →  fnmatch: cosmonapse.[^.]+.TASK
        # We do a simpler manual approach:
        p_parts = pattern.split(".")
        s_parts = subject.split(".")

        i = 0
        j = 0
        while i < len(p_parts) and j < len(s_parts):
            if p_parts[i] == ">":
                return True  # matches rest of subject
            if p_parts[i] == "*":
                i += 1
                j += 1
                continue
            if p_parts[i] != s_parts[j]:
                return False
            i += 1
            j += 1

        return i == len(p_parts) and j == len(s_parts)

    async def publish(self, subject: str, signal: Signal) -> None:
        assert self._connected, "Synapse not connected"
        await self._deliver(subject, signal)

    async def _deliver(self, subject: str, signal: Signal) -> None:
        """Fan out signal to all matching subscribers, respecting queue groups."""
        # group_name → list of handlers that match this subject in that group
        queue_groups: dict[str, list[MessageHandler]] = defaultdict(list)
        solo_handlers: list[MessageHandler] = []

        for pattern, entries in self._subs.items():
            if not self._matches(pattern, subject):
                continue
            for _id, group, handler in entries:
                if group is None:
                    solo_handlers.append(handler)
                else:
                    queue_groups[group].append(handler)

        def _ensure_coro(handler: MessageHandler, sig: Signal):
            """Call handler and return a coroutine regardless of whether it's async or sync."""
            import inspect
            result = handler(sig)
            if inspect.iscoroutine(result):
                return result
            # Sync handler  -  wrap in a coroutine
            async def _wrap():
                return result
            return _wrap()

        # Solo handlers all receive the message
        tasks = [asyncio.create_task(_ensure_coro(h, signal)) for h in solo_handlers]

        # Queue groups: strict round-robin using per-group counter
        for group, handlers in queue_groups.items():
            if handlers:
                idx = self._rr_counters[group] % len(handlers)
                self._rr_counters[group] += 1
                tasks.append(asyncio.create_task(_ensure_coro(handlers[idx], signal)))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def subscribe(
        self,
        subject: str,
        handler: MessageHandler,
        *,
        queue_group: str | None = None,
    ) -> MemorySubscription:
        assert self._connected, "Synapse not connected"
        hid = self._next_id()
        self._subs[subject].append((hid, queue_group, handler))
        return MemorySubscription(self, subject, hid)

    async def request(
        self,
        subject: str,
        signal: Signal,
        *,
        timeout_s: float = 5.0,
    ) -> Signal:
        assert self._connected, "Synapse not connected"
        reply_subject = f"_INBOX.{signal.id}"
        fut: asyncio.Future[Signal] = asyncio.get_running_loop().create_future()

        async def _reply_handler(reply: Signal) -> None:
            if not fut.done():
                fut.set_result(reply)

        sub = await self.subscribe(reply_subject, _reply_handler)
        try:
            # Attach reply-to in meta so the receiver knows where to respond
            enriched = signal.model_copy(
                update={"meta": {**signal.meta, "_reply_to": reply_subject}}
            )
            await self.publish(subject, enriched)
            return await asyncio.wait_for(fut, timeout=timeout_s)
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"No reply received on {reply_subject!r} within {timeout_s}s"
            )
        finally:
            await sub.unsubscribe()

    async def reply_to(self, original: Signal, reply: Signal) -> None:
        """
        Convenience: send reply to the _reply_to subject stored in original.meta.
        Used by request/reply responders.
        """
        reply_to = original.meta.get("_reply_to")
        if not reply_to:
            raise ValueError("Signal has no _reply_to in meta  -  not a request signal")
        await self.publish(reply_to, reply)
