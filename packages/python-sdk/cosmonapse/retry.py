"""
cosmonapse.retry
~~~~~~~~~~~~~~~~
Declarative retry policy for the dispatch + wait shape.

A :class:`RetryStrategy` is consumed by ``Dendrite.dispatch_and_wait(retry=...)``
and ``Dendrite.run_with_retry(...)``. Retry only makes sense for the
request/reply shape, where the Dendrite owns the whole arc
(dispatch -> wait -> close) and can transparently re-dispatch. The streaming
shapes (``dispatch`` / ``dispatch_and_subscribe``) hand the live Pathway back
to the caller, so retry there would orphan the caller's subscriptions - use a
resilient-pathway wrapper for that instead.

"Stuck" is detected as: no terminal Signal within ``timeout_s`` (an
``asyncio.TimeoutError`` from ``Pathway.wait``), the Pathway closing before a
terminal (``PathwayClosedError``), or a returned ERROR whose payload is
``recoverable``. New-trace retries STOP the abandoned attempt before launching
the next one, so a stalled worker can't keep running (or keep writing to an
Engram) behind the retry.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field

from cosmonapse.envelope import Signal, SignalType
from cosmonapse.pathway import PathwayClosedError


def _no_backoff(attempt: int) -> float:
    return 0.0


def default_retry_on(outcome: object) -> bool:
    """Default predicate: retry on timeout, on a Pathway that closed before a
    terminal, or on a returned ERROR flagged ``recoverable``. A FINAL /
    AGENT_OUTPUT / CLARIFICATION / PERMISSION is never retried - those are a
    result or a decision the caller must handle."""
    if isinstance(outcome, (asyncio.TimeoutError, PathwayClosedError)):
        return True
    if isinstance(outcome, Signal):
        return (
            outcome.type is SignalType.ERROR
            and bool((outcome.payload or {}).get("recoverable"))
        )
    return False


@dataclass(frozen=True)
class RetryStrategy:
    """How to retry a dispatched workflow.

    Parameters
    ----------
    max_attempts   Total tries, including the first (>= 1).
    timeout_s      Per-attempt terminal timeout. ``None`` falls back to the
                   caller's ``timeout_s``.
    backoff        ``attempt -> seconds`` to sleep before the next try
                   (attempt is 0-based). Default: no delay.
    retry_on       ``outcome -> bool`` where outcome is the resolved Signal or
                   the raised exception. Default: :func:`default_retry_on`.
    new_trace      ``True`` (default): each attempt gets a fresh trace and the
                   abandoned one is STOPped. ``False``: reuse the caller's
                   trace (no preemption).
    rollback_on_retry  When STOPping an abandoned attempt, also roll back its
                   Engram writes via the saga journal. Default ``False``.
    on_retry       Optional ``(attempt, outcome) -> None`` hook for
                   logging/metrics, fired just before a re-dispatch.
    reason         Reason string carried on the preemptive STOP.
    """

    max_attempts: int = 3
    timeout_s: float | None = 30.0
    backoff: Callable[[int], float] = _no_backoff
    retry_on: Callable[[object], bool] = field(default=default_retry_on)
    new_trace: bool = True
    rollback_on_retry: bool = False
    on_retry: Callable[[int, object], None] | None = None
    reason: str = "retry"

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("RetryStrategy.max_attempts must be >= 1")
