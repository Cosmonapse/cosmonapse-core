"""
cosmonapse.synapse.base
~~~~~~~~~~~~~~~~~~~~~~~~~
Synapse interface that all adapters must implement.

The interface is intentionally narrow  -  five methods only.
Adapters translate these into whatever the underlying broker requires.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

from cosmonapse.envelope import Signal

# A subscriber callback receives each Signal as it arrives
MessageHandler = Callable[[Signal], Awaitable[None]]


class Synapse(ABC):
    """
    Abstract base for all Cosmonapse synapse adapters.

    Implementations: MemorySynapse, NatsSynapse (post-v1), KafkaSynapse (post-v1)

    Subject convention (see ENVELOPE_SPEC.md §10):
      cosmonapse.<namespace>.<type>
      e.g. cosmonapse.team_a.TASK
           cosmonapse.team_a.AGENT_OUTPUT
           cosmonapse.>           (subscribe all)
    """

    @abstractmethod
    async def connect(self) -> None:
        """Establish connection to the broker / initialise in-memory state."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Gracefully disconnect and release resources."""
        ...

    @abstractmethod
    async def publish(self, subject: str, signal: Signal) -> None:
        """
        Publish a Signal to the given subject.

        The signal is serialised with signal.encode() before transmission.
        """
        ...

    @abstractmethod
    async def subscribe(
        self,
        subject: str,
        handler: MessageHandler,
        *,
        queue_group: str | None = None,
    ) -> Subscription:
        """
        Subscribe to a subject pattern.

        subject        -  exact subject or wildcard (e.g. 'cosmonapse.>' for all)
        handler        -  async callback invoked for each received Signal
        queue_group    -  if set, only one subscriber in the group receives each message
                        (load-balancing across multiple Cortex instances).
                        Doppler subscribers must NOT use a queue_group.
        """
        ...

    @abstractmethod
    async def request(
        self,
        subject: str,
        signal: Signal,
        *,
        timeout_s: float = 5.0,
    ) -> Signal:
        """
        Publish a Signal and wait for exactly one reply.

        Used for request/reply patterns in routing (e.g. bid collection).
        """
        ...


class Subscription(ABC):
    """Handle for an active subscription. Used to unsubscribe cleanly."""

    @abstractmethod
    async def unsubscribe(self) -> None:
        """Stop receiving messages on this subscription."""
        ...
