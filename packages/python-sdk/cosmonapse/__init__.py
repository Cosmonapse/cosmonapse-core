"""
Cosmonapse Python SDK
~~~~~~~~~~~~~~~~~~~~~
Event-driven Agent-to-Agent protocol primitives.

Layers
------
  Neuron        A pure async function - the agent itself, zero protocol
                knowledge. Optionally constructed via Neuron(source=...)
                provider factories (Ollama, HuggingFace TGI / vLLM /
                OpenAI-compat, Flask/WSGI app, MCP server).
  Axon          Agent-side interface. Declares capabilities and validates
                the Neuron's raw output into a Signal envelope. The
                Neuron is unaware of Axon, Dendrite, or Synapse.
  Dendrite      Synapse-side participant. Owns routing decisions and
                exposes the aggregate of its Axons' capabilities. Has a
                ``role`` ("orchestrator" can dispatch; "worker" hosts
                Axons only). Synapse is the only required constructor
                argument; everything else is opt-in.
  Synapse       Message bus adapter (memory / dev / NATS / Kafka).
                Caller-owned; built and closed externally.

Cortex
------
``Cortex`` is a back-compat alias for ``Dendrite``. There is no
separate orchestrator class - any orchestrator-role Dendrite has the
full dispatch / emit / handler surface.

Dispatch shapes (orchestrator-role only)
----------------------------------------
* ``dispatch_task(neuron=..., input=...)`` - fire-and-forget addressed
  TASK; returns the emitted Signal.
* ``dispatch(neuron|capabilities=..., input=..., scope=...)`` - returns
  a :class:`Pathway` scoped to the new trace.
* ``dispatch_and_wait(...)`` - sugar: dispatch, block until first
  terminal Signal (AGENT_OUTPUT / CLARIFICATION / PERMISSION / ERROR / FINAL),
  close, return the Signal.
* ``dispatch_and_subscribe(...)`` - sugar: dispatch, return the live
  Pathway so the caller can attach ``@pw.on(SignalType.X)`` handlers
  without awaiting.
* ``dispatch_offer(input=..., capabilities=..., deadline_ms=..., select=...)``
  - competitive bidding via TASK_OFFER / BID / TASK_AWARDED. Returns a
  Pathway scoped to the awarded workflow. Selection: ``"first_bid"``,
  ``"lowest_cost"``, ``"highest_confidence"``.

Capability-routed dispatch publishes on a separate subject
(``cosmonapse.<ns>.TASK.routed``) with a queue group keyed on each
Dendrite's aggregate capabilities - identical-cap-profile Dendrites
load-balance and the broker delivers each TASK exactly once within
the group. Addressed TASKs continue to broadcast on
``cosmonapse.<ns>.TASK``.

Cognition surface
-----------------
Every cognition signal type (PLAN, THOUGHT_DELTA, TOOL_CALL,
TOOL_RESULT, MEMORY_APPEND, CRITIQUE, ESCALATION, CONSENSUS,
CONTEXT_SYNC) has a matching ``emit_*`` method and ``on_*`` decorator
on Dendrite. Decorators accept optional filter kwargs - ``neuron=``,
``capability=``, ``trace_id=`` - so a handler can be scoped without
manual filtering inside the body. ``on_trace(trace_id, *types)``
narrows a handler to one workflow across whichever signal types are
listed.

The role guard sits on ``emit()`` itself, so every cognition emitter
funnels through it and worker-role Dendrites are blocked from
emitting orchestration signals (except ``bid()`` which uses the
private publish path - bidding is how workers participate in
capability routing).

Pathway
-------
``dendrite.dispatch(...)`` returns a :class:`Pathway` - a per-trace
event handle with three consumption shapes on the same primitive:
``await pw.wait()`` for sequential request/reply, ``@pw.on(...)`` for
reactive trace-scoped callbacks, and ``async for sig in pw:`` for
streaming iteration. ``observe_pathway(trace_id)`` opens a Pathway
in observer role for a trace another peer started.

``Pathway(scope=...)`` filters which Signal types are delivered:
``"all"`` (default) sees every PATHWAY_TYPES Signal on the trace;
``"terminal"`` delivers only FINAL / ERROR / CLARIFICATION /
PERMISSION - the decentralised pattern where intermediate orchestration
is handled peer-to-peer and the Cortex only wakes for things that demand
attention (a conclusion, or a decision the workflow is blocked on). FINAL
/ ERROR always reach auto-close regardless of scope.

Pathways auto-close on FINAL or ERROR, are closed by
``Dendrite.stop()``, and never alter what crosses the wire. The
entire surface is opt-in additive sugar over the existing
``dispatch_task`` / ``on_agent_output`` API.
"""

# Single source of truth for the version is the installed distribution
# metadata, which hatch-vcs derives from the `vX.Y.Z` git tag at build time
# (see pyproject.toml). Nothing here is hand-edited per release.
from importlib.metadata import PackageNotFoundError as _PkgNotFound
from importlib.metadata import version as _dist_version

from cosmonapse._url import connect_synapse, synapse_from_url
from cosmonapse.axon import (
    COSMO_INTENT_SYSTEM_PROMPT,
    Axon,
    ContextFetcher,
    NeuronFn,
)
from cosmonapse.dendrite import (
    Cortex,
    CortexProtocolError,
    Dendrite,
    DendriteProtocolError,
)
from cosmonapse.effector import (
    TOOL_STANDARDS,
    Effector,
    EffectorBinding,
    EffectorCancelled,
    EffectorClient,
    EffectorError,
    EffectorNotBound,
    EffectorOverloaded,
    EffectorTimeout,
    ToolOutcome,
)
from cosmonapse.engram import (
    Engram,
    EngramBinding,
    EngramCancelled,
    EngramClient,
    EngramError,
    EngramNotBound,
    EngramOverloaded,
    EngramTimeout,
    Hit,
    ImprintReceipt,
    InMemoryEngram,
    PostgresEngram,
    RecallResult,
    SqliteEngram,
)
from cosmonapse.envelope import (
    AXON_TYPES,
    SYNAPSE_TYPES,
    Directed,
    Signal,
    SignalType,
    agent_output_signal,
    bid_signal,
    clarification_answer_signal,
    clarification_signal,
    consensus_signal,
    context_sync_signal,
    critique_signal,
    deregister_signal,
    directed_to,
    discover_signal,
    error_signal,
    escalation_signal,
    final_signal,
    heartbeat_signal,
    imprint_signal,
    imprinted_signal,
    memory_append_signal,
    new_engram_id,
    new_event_id,
    new_trace_id,
    permission_decision_signal,
    permission_signal,
    plan_signal,
    recall_signal,
    recalled_signal,
    register_signal,
    stop_signal,
    stopped_signal,
    task_offer_signal,
    task_signal,
    thought_delta_signal,
    tool_call_signal,
    tool_result_signal,
)
from cosmonapse.neuron import STANDARD_MCP_SERVERS, Neuron
from cosmonapse.pathway import PATHWAY_TYPES, Pathway, PathwayClosedError
from cosmonapse.retry import RetryStrategy, default_retry_on
from cosmonapse.storage import (
    MemoryRegistryStore,
    NeuronRecord,
    PostgresRegistryStore,
    RegistryStore,
    SqliteRegistryStore,
)
from cosmonapse.synapse import (
    DevSynapse,
    DevSynapseServer,
    KafkaSynapse,
    MemorySynapse,
    NatsSynapse,
    Synapse,
)

try:
    __version__ = _dist_version("cosmonapse")
except _PkgNotFound:  # running from a source tree that isn't installed
    __version__ = "0.0.0.dev0"

del _PkgNotFound, _dist_version

# Grouped by concept rather than alphabetically  -  this list doubles as the
# public API map.
__all__ = [  # noqa: RUF022
    "Signal",
    "SignalType",
    "Directed",
    "directed_to",
    "AXON_TYPES",
    "SYNAPSE_TYPES",
    "new_event_id",
    "new_trace_id",
    "task_signal",
    "agent_output_signal",
    "clarification_signal",
    "clarification_answer_signal",
    "permission_signal",
    "permission_decision_signal",
    "final_signal",
    "error_signal",
    "stop_signal",
    "stopped_signal",
    "RetryStrategy",
    "default_retry_on",
    "register_signal",
    "deregister_signal",
    "heartbeat_signal",
    "memory_append_signal",
    "task_offer_signal",
    "bid_signal",
    "critique_signal",
    "discover_signal",
    "plan_signal",
    "thought_delta_signal",
    "tool_call_signal",
    "tool_result_signal",
    "escalation_signal",
    "consensus_signal",
    "context_sync_signal",
    "Neuron",
    "STANDARD_MCP_SERVERS",
    "Axon",
    "COSMO_INTENT_SYSTEM_PROMPT",
    "NeuronFn",
    "ContextFetcher",
    "Dendrite",
    "DendriteProtocolError",
    "Cortex",
    "CortexProtocolError",
    "Pathway",
    "PathwayClosedError",
    "PATHWAY_TYPES",
    "NeuronRecord",
    "Synapse",
    "MemorySynapse",
    "DevSynapse",
    "DevSynapseServer",
    "NatsSynapse",
    "KafkaSynapse",
    "RegistryStore",
    "MemoryRegistryStore",
    "SqliteRegistryStore",
    "PostgresRegistryStore",
    "synapse_from_url",
    "connect_synapse",
    # Effector
    "Effector",
    "EffectorBinding",
    "EffectorClient",
    "TOOL_STANDARDS",
    "EffectorError",
    "EffectorCancelled",
    "EffectorNotBound",
    "EffectorOverloaded",
    "EffectorTimeout",
    "ToolOutcome",
    # Engram
    "new_engram_id",
    "recall_signal",
    "recalled_signal",
    "imprint_signal",
    "imprinted_signal",
    "Engram",
    "EngramBinding",
    "EngramClient",
    "EngramError",
    "EngramCancelled",
    "EngramNotBound",
    "EngramOverloaded",
    "EngramTimeout",
    "Hit",
    "RecallResult",
    "ImprintReceipt",
    "InMemoryEngram",
    "SqliteEngram",
    "PostgresEngram",
]
