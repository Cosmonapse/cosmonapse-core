/**
 * @cosmonapse/sdk  -  typed signal builders
 *
 * Convenience constructors for the common Signal types. These are NOT required
 * by the protocol  -  the protocol only requires a valid Signal with the correct
 * `type` and `payload`. They mirror the helpers in `cosmonapse.envelope`.
 *
 * Addressing is carried by the envelope `directed` field (see {@link Directed}).
 * Builders accept a `directed` (full or partial) rather than a bare `neuron`
 * string; higher-level helpers (e.g. `Dendrite.dispatchTask`) keep a `neuron`
 * argument for ergonomics and wrap it into `{ id: neuron }`.
 */

import {
  createSignal,
  newTraceId,
  normalizeDirected,
  SignalType,
  type DirectedInput,
  type Json,
  type Signal,
} from "./envelope.js";

/** [C] Dispatch a unit of work to a Neuron. */
export function taskSignal(args: {
  input: Json;
  traceId?: string;
  parentId?: string | null;
  directed?: DirectedInput;
  contextRef?: string;
  capabilities?: string[];
  /** Terminal-handler finalize: the worker Dendrite that runs the addressed
   *  (or routed) Axon promotes a successful AGENT_OUTPUT by also emitting
   *  FINAL on the trace. Set automatically by `dispatch({ scope: "terminal" })`. */
  finalize?: boolean;
  meta?: Json;
}): Signal {
  const payload: Json = { input: args.input };
  if (args.contextRef) payload["context_ref"] = args.contextRef;
  if (args.capabilities) payload["capabilities"] = args.capabilities;
  if (args.finalize) payload["finalize"] = true;
  return createSignal({
    type: SignalType.TASK,
    trace_id: args.traceId ?? newTraceId(),
    parent_id: args.parentId ?? null,
    directed: args.directed ?? null,
    payload,
    meta: args.meta ?? {},
  });
}

/** [A] Wrap a Neuron's raw output in a neutral AGENT_OUTPUT envelope. */
export function agentOutputSignal(args: {
  traceId: string;
  parentId: string;
  directed?: DirectedInput;
  output: Json;
  meta?: Json;
}): Signal {
  return createSignal({
    type: SignalType.AGENT_OUTPUT,
    trace_id: args.traceId,
    parent_id: args.parentId,
    directed: args.directed ?? null,
    payload: { output: args.output },
    meta: args.meta ?? {},
  });
}

/** [A] The Neuron needs more information before it can complete the task. */
export function clarificationSignal(args: {
  traceId: string;
  parentId: string;
  directed?: DirectedInput;
  question: string;
  context?: Json;
  meta?: Json;
}): Signal {
  const payload: Json = { question: args.question };
  if (args.context) payload["context"] = args.context;
  return createSignal({
    type: SignalType.CLARIFICATION,
    trace_id: args.traceId,
    parent_id: args.parentId,
    directed: args.directed ?? null,
    payload,
    meta: args.meta ?? {},
  });
}

/** [A] A Neuron asks permission to perform `action` before doing it. */
export function permissionSignal(args: {
  traceId: string;
  parentId: string;
  directed?: DirectedInput;
  action: string;
  scope?: Json;
  reason?: string;
  context?: Json;
  meta?: Json;
}): Signal {
  const payload: Json = { action: args.action };
  if (args.scope) payload["scope"] = args.scope;
  if (args.reason !== undefined) payload["reason"] = args.reason;
  if (args.context) payload["context"] = args.context;
  return createSignal({
    type: SignalType.PERMISSION,
    trace_id: args.traceId,
    parent_id: args.parentId,
    directed: args.directed ?? null,
    payload,
    meta: args.meta ?? {},
  });
}

/** [C] Verdict on a PERMISSION request. `parentId` MUST be the PERMISSION's id. */
export function permissionDecisionSignal(args: {
  traceId: string;
  parentId: string;
  granted: boolean;
  directed?: DirectedInput;
  reason?: string;
  ttlMs?: number;
  meta?: Json;
}): Signal {
  const payload: Json = { granted: args.granted };
  if (args.reason !== undefined) payload["reason"] = args.reason;
  if (args.ttlMs !== undefined) payload["ttl_ms"] = args.ttlMs;
  return createSignal({
    type: SignalType.PERMISSION_DECISION,
    trace_id: args.traceId,
    parent_id: args.parentId,
    directed: args.directed ?? null,
    payload,
    meta: args.meta ?? {},
  });
}

/** [C] Answer to a blocking CLARIFICATION. `parentId` MUST be the CLARIFICATION's id. */
export function clarificationAnswerSignal(args: {
  traceId: string;
  parentId: string;
  answer: unknown;
  directed?: DirectedInput;
  meta?: Json;
}): Signal {
  return createSignal({
    type: SignalType.CLARIFICATION_ANSWER,
    trace_id: args.traceId,
    parent_id: args.parentId,
    directed: args.directed ?? null,
    payload: { answer: args.answer },
    meta: args.meta ?? {},
  });
}

/** [C] Workflow concluded. `result` carries the terminal output. */
export function finalSignal(args: {
  traceId: string;
  parentId: string;
  result: Json;
  directed?: DirectedInput;
  cost?: Json;
  meta?: Json;
}): Signal {
  const payload: Json = { result: args.result };
  if (args.cost) payload["cost"] = args.cost;
  return createSignal({
    type: SignalType.FINAL,
    trace_id: args.traceId,
    parent_id: args.parentId,
    directed: args.directed ?? null,
    payload,
    meta: args.meta ?? {},
  });
}

/** [A][C] Something went wrong. `recoverable` lets the Cortex retry or reroute. */
export function errorSignal(args: {
  traceId: string;
  code: string;
  message: string;
  parentId?: string | null;
  directed?: DirectedInput;
  recoverable?: boolean;
  meta?: Json;
}): Signal {
  return createSignal({
    type: SignalType.ERROR,
    trace_id: args.traceId,
    parent_id: args.parentId ?? null,
    directed: args.directed ?? null,
    payload: {
      code: args.code,
      message: args.message,
      recoverable: args.recoverable ?? false,
    },
    meta: args.meta ?? {},
  });
}

/**
 * [A] A participant connecting to the Synapse and declaring its capabilities.
 *
 * Both Neurons and Engrams register the same way: `directed.id` is the
 * participant id, `directed.type` its kind (`neuron_kind` for Neurons,
 * `engram_kind` for Engrams), and `directed.capabilities` its capability list.
 *
 * Every REGISTER carries one universal discriminator, `payload.role`
 * (`"neuron"` or `"engram"`): the single field every consumer (Dendrite
 * registry, Prism, doppler) checks to classify the participant. `role`
 * defaults from the `engram` flag when omitted. The legacy
 * `payload.engram = true` marker is still emitted for Engrams as an alias.
 * `capabilities` is mirrored into the payload for registry stores; when
 * omitted it falls back to `directed.capabilities`.
 */
export function registerSignal(args: {
  directed: DirectedInput;
  capabilities?: string[];
  version?: string;
  engram?: boolean;
  role?: "neuron" | "engram";
  meta?: Json;
}): Signal {
  const caps = args.capabilities ?? args.directed?.capabilities ?? [];
  const role = args.role ?? (args.engram ? "engram" : "neuron");
  const payload: Json = { role, capabilities: caps };
  if (args.version) payload["version"] = args.version;
  if (args.engram || role === "engram") payload["engram"] = true;
  return createSignal({
    type: SignalType.REGISTER,
    trace_id: newTraceId(), // management signals get their own trace
    directed: args.directed ?? null,
    payload,
    meta: args.meta ?? {},
  });
}

/** [A] A participant disconnecting from the Synapse. */
export function deregisterSignal(args: {
  directed: DirectedInput;
  reason?: string;
  meta?: Json;
}): Signal {
  const payload: Json = {};
  if (args.reason) payload["reason"] = args.reason;
  return createSignal({
    type: SignalType.DEREGISTER,
    trace_id: newTraceId(),
    directed: args.directed ?? null,
    payload,
    meta: args.meta ?? {},
  });
}

/** [A] Periodic liveness signal from a participant. */
export function heartbeatSignal(args: {
  directed: DirectedInput;
  status?: string;
  meta?: Json;
}): Signal {
  return createSignal({
    type: SignalType.HEARTBEAT,
    trace_id: newTraceId(),
    directed: args.directed ?? null,
    payload: { status: args.status ?? "ok" },
    meta: args.meta ?? {},
  });
}

/** [C] Write a value to the shared Engram under the given key. */
export function memoryAppendSignal(args: {
  traceId: string;
  parentId: string;
  key: string;
  value: unknown;
  directed?: DirectedInput;
  meta?: Json;
}): Signal {
  return createSignal({
    type: SignalType.MEMORY_APPEND,
    trace_id: args.traceId,
    parent_id: args.parentId,
    directed: args.directed ?? null,
    payload: { key: args.key, value: args.value },
    meta: args.meta ?? {},
  });
}

/** [C] Broadcast a task to candidate Neurons for competitive bidding. */
export function taskOfferSignal(args: {
  traceId: string;
  input: Json;
  parentId?: string | null;
  capabilities?: string[];
  deadlineMs?: number;
  meta?: Json;
}): Signal {
  const payload: Json = { input: args.input };
  if (args.capabilities) payload["capabilities"] = args.capabilities;
  if (args.deadlineMs !== undefined) payload["deadline_ms"] = args.deadlineMs;
  return createSignal({
    type: SignalType.TASK_OFFER,
    trace_id: args.traceId,
    parent_id: args.parentId ?? null,
    payload,
    meta: args.meta ?? {},
  });
}

/** [C] Cortex bids on behalf of a Neuron in response to a TASK_OFFER. */
export function bidSignal(args: {
  traceId: string;
  parentId: string;
  directed?: DirectedInput;
  cost: number;
  etaMs?: number;
  confidence?: number;
  meta?: Json;
}): Signal {
  const payload: Json = { cost: args.cost };
  if (args.etaMs !== undefined) payload["eta_ms"] = args.etaMs;
  if (args.confidence !== undefined) payload["confidence"] = args.confidence;
  return createSignal({
    type: SignalType.BID,
    trace_id: args.traceId,
    parent_id: args.parentId,
    directed: args.directed ?? null,
    payload,
    meta: args.meta ?? {},
  });
}

/** [C] Award a TASK_OFFER to one bidder. The winning Axon's Dendrite treats
 *  this exactly like a TASK: `input` is the work payload, `directed`
 *  addresses the winner. `winningBid` carries the accepted bid for
 *  observability; `finalize` propagates the terminal-handler-finalize tag
 *  into the TASK the winner's Dendrite synthesises. */
export function taskAwardedSignal(args: {
  traceId: string;
  parentId: string;
  input: Json;
  directed?: DirectedInput;
  winningBid?: Json;
  contextRef?: string;
  finalize?: boolean;
  meta?: Json;
}): Signal {
  const payload: Json = { input: args.input };
  if (args.winningBid !== undefined) payload["winning_bid"] = args.winningBid;
  if (args.contextRef !== undefined) payload["context_ref"] = args.contextRef;
  if (args.finalize) payload["finalize"] = true;
  return createSignal({
    type: SignalType.TASK_AWARDED,
    trace_id: args.traceId,
    parent_id: args.parentId,
    directed: args.directed ?? null,
    payload,
    meta: args.meta ?? {},
  });
}

/** [C/A] Decline a TASK_OFFER. Producers emit this for losing bidders after
 *  picking a winner (informational); workers may emit it proactively. */
export function taskDeclinedSignal(args: {
  traceId: string;
  parentId: string;
  directed?: DirectedInput;
  reason?: string;
  meta?: Json;
}): Signal {
  const payload: Json = {};
  if (args.reason !== undefined) payload["reason"] = args.reason;
  return createSignal({
    type: SignalType.TASK_DECLINED,
    trace_id: args.traceId,
    parent_id: args.parentId,
    directed: args.directed ?? null,
    payload,
    meta: args.meta ?? {},
  });
}

/** [C] Peer review of another Neuron's output. `verdict`: 'pass' | 'fail' | 'revise'. */
export function critiqueSignal(args: {
  traceId: string;
  parentId: string;
  targetEventId: string;
  issues: Json[];
  verdict: "pass" | "fail" | "revise";
  directed?: DirectedInput;
  meta?: Json;
}): Signal {
  return createSignal({
    type: SignalType.CRITIQUE,
    trace_id: args.traceId,
    parent_id: args.parentId,
    directed: args.directed ?? null,
    payload: {
      target_event_id: args.targetEventId,
      issues: args.issues,
      verdict: args.verdict,
    },
    meta: args.meta ?? {},
  });
}

/** [C] Structured plan emitted before execution. */
export function planSignal(args: {
  traceId: string;
  parentId: string;
  steps: Json[];
  rationale?: string;
  directed?: DirectedInput;
  meta?: Json;
}): Signal {
  const payload: Json = { steps: args.steps };
  if (args.rationale !== undefined) payload["rationale"] = args.rationale;
  return createSignal({
    type: SignalType.PLAN,
    trace_id: args.traceId,
    parent_id: args.parentId,
    directed: args.directed ?? null,
    payload,
    meta: args.meta ?? {},
  });
}

/** [C] Streaming reasoning chunk. */
export function thoughtDeltaSignal(args: {
  traceId: string;
  parentId: string;
  delta: string;
  seq?: number;
  directed?: DirectedInput;
  meta?: Json;
}): Signal {
  const payload: Json = { delta: args.delta };
  if (args.seq !== undefined) payload["seq"] = args.seq;
  return createSignal({
    type: SignalType.THOUGHT_DELTA,
    trace_id: args.traceId,
    parent_id: args.parentId,
    directed: args.directed ?? null,
    payload,
    meta: args.meta ?? {},
  });
}

/** [C] Neuron invoking an external tool. */
export function toolCallSignal(args: {
  traceId: string;
  parentId: string;
  tool: string;
  args: Json;
  callId?: string;
  directed?: DirectedInput;
  meta?: Json;
}): Signal {
  const payload: Json = { tool: args.tool, args: args.args };
  if (args.callId !== undefined) payload["call_id"] = args.callId;
  return createSignal({
    type: SignalType.TOOL_CALL,
    trace_id: args.traceId,
    parent_id: args.parentId,
    directed: args.directed ?? null,
    payload,
    meta: args.meta ?? {},
  });
}

/** [C] Result returned from a tool. Set exactly one of `result` / `error`. */
export function toolResultSignal(args: {
  traceId: string;
  parentId: string;
  tool: string;
  result?: unknown;
  error?: string;
  callId?: string;
  directed?: DirectedInput;
  meta?: Json;
}): Signal {
  const payload: Json = { tool: args.tool };
  if (args.result !== undefined) payload["result"] = args.result as Json[string];
  if (args.error !== undefined) payload["error"] = args.error;
  if (args.callId !== undefined) payload["call_id"] = args.callId;
  return createSignal({
    type: SignalType.TOOL_RESULT,
    trace_id: args.traceId,
    parent_id: args.parentId,
    directed: args.directed ?? null,
    payload,
    meta: args.meta ?? {},
  });
}

/** [C] Escalate a task or sub-decision to a higher-authority Neuron. */
export function escalationSignal(args: {
  traceId: string;
  parentId: string;
  reason: string;
  target?: string;
  context?: Json;
  directed?: DirectedInput;
  meta?: Json;
}): Signal {
  const payload: Json = { reason: args.reason };
  if (args.target !== undefined) payload["target"] = args.target;
  if (args.context !== undefined) payload["context"] = args.context;
  return createSignal({
    type: SignalType.ESCALATION,
    trace_id: args.traceId,
    parent_id: args.parentId,
    directed: args.directed ?? null,
    payload,
    meta: args.meta ?? {},
  });
}

/** [C] Record a consensus outcome among multiple Neurons. */
export function consensusSignal(args: {
  traceId: string;
  parentId: string;
  members: string[];
  verdict: string;
  votes?: Json;
  directed?: DirectedInput;
  meta?: Json;
}): Signal {
  const payload: Json = { members: args.members, verdict: args.verdict };
  if (args.votes !== undefined) payload["votes"] = args.votes;
  return createSignal({
    type: SignalType.CONSENSUS,
    trace_id: args.traceId,
    parent_id: args.parentId,
    directed: args.directed ?? null,
    payload,
    meta: args.meta ?? {},
  });
}

/** [C] Share/synchronise context across Neurons. */
export function contextSyncSignal(args: {
  traceId: string;
  parentId: string;
  snapshot: Json;
  version?: string;
  directed?: DirectedInput;
  meta?: Json;
}): Signal {
  const payload: Json = { snapshot: args.snapshot };
  if (args.version !== undefined) payload["version"] = args.version;
  return createSignal({
    type: SignalType.CONTEXT_SYNC,
    trace_id: args.traceId,
    parent_id: args.parentId,
    directed: args.directed ?? null,
    payload,
    meta: args.meta ?? {},
  });
}

/**
 * [C] Solicit a REGISTER snapshot from peers on a namespace. `neuron` /
 * `capabilities` are payload filter fields (which participants to discover),
 * not envelope addressing.
 */
export function discoverSignal(args: {
  neuron?: string;
  capabilities?: string[];
  traceId?: string;
  parentId?: string | null;
  meta?: Json;
} = {}): Signal {
  const payload: Json = {};
  if (args.neuron !== undefined) payload["neuron"] = args.neuron;
  if (args.capabilities !== undefined) payload["capabilities"] = args.capabilities;
  return createSignal({
    type: SignalType.DISCOVER,
    trace_id: args.traceId ?? newTraceId(),
    parent_id: args.parentId ?? null,
    payload,
    meta: args.meta ?? {},
  });
}

// ---------------------------------------------------------------------------
// Engram signal builders (see ENGRAM_DESIGN.md §4)
// ---------------------------------------------------------------------------

const RECALL_MODES = new Set(["first", "merge", "all"]);
const IMPRINT_OPS = new Set(["add", "append", "merge", "upsert", "delete"]);

/**
 * [D] Memory-recall request. Inherits `traceId` from the containing TASK and
 * MUST be addressed: `directed` needs at least one of `id` (engram_id) or
 * `type` (engram_kind). `recallMode` controls fan-out: `"first"` (one winner),
 * `"merge"` (caller merges all by deadline), `"all"` (stream each).
 */
export function recallSignal(args: {
  traceId: string;
  parentId: string;
  directed: DirectedInput;
  query: Json;
  filters?: Json;
  contextRef?: string;
  deadlineMs?: number;
  minConfidence?: number;
  recallMode?: "first" | "merge" | "all";
  meta?: Json;
}): Signal {
  const directed = normalizeDirected(args.directed);
  if (!directed || (!directed.id && !directed.type)) {
    throw new Error(
      "recallSignal requires directed.id (engram_id) or directed.type (engram_kind)",
    );
  }
  const mode = args.recallMode ?? "first";
  if (!RECALL_MODES.has(mode)) {
    throw new Error(`recallMode must be 'first' | 'merge' | 'all', got '${mode}'`);
  }
  const payload: Json = { query: args.query, recall_mode: mode };
  if (args.filters !== undefined) payload["filters"] = args.filters;
  if (args.contextRef !== undefined) payload["context_ref"] = args.contextRef;
  if (args.deadlineMs !== undefined) payload["deadline_ms"] = args.deadlineMs;
  if (args.minConfidence !== undefined) payload["min_confidence"] = args.minConfidence;
  return createSignal({
    type: SignalType.RECALL,
    trace_id: args.traceId,
    parent_id: args.parentId,
    directed: args.directed,
    payload,
    meta: args.meta ?? {},
  });
}

/** [D] Response from one Engram to a RECALL. `parentId` MUST be the RECALL's id. */
export function recalledSignal(args: {
  traceId: string;
  parentId: string;
  engramId: string;
  hits: Json[];
  truncated?: boolean;
  tookMs?: number;
  directed?: DirectedInput;
  meta?: Json;
}): Signal {
  const payload: Json = {
    engram_id: args.engramId,
    hits: args.hits,
    truncated: args.truncated ?? false,
  };
  if (args.tookMs !== undefined) payload["took_ms"] = args.tookMs;
  return createSignal({
    type: SignalType.RECALLED,
    trace_id: args.traceId,
    parent_id: args.parentId,
    directed: args.directed ?? null,
    payload,
    meta: args.meta ?? {},
  });
}

/**
 * [D] Memory-write request. `op` is one of add | append | merge | upsert |
 * delete; `mergeKey` is required for merge/upsert. MUST be addressed.
 */
export function imprintSignal(args: {
  traceId: string;
  parentId: string;
  directed: DirectedInput;
  op: "add" | "append" | "merge" | "upsert" | "delete";
  entry: Json;
  mergeKey?: string;
  meta?: Json;
}): Signal {
  if (!IMPRINT_OPS.has(args.op)) {
    throw new Error(`imprint op must be one of ${[...IMPRINT_OPS].join(" | ")}, got '${args.op}'`);
  }
  const directed = normalizeDirected(args.directed);
  if (!directed || (!directed.id && !directed.type)) {
    throw new Error(
      "imprintSignal requires directed.id (engram_id) or directed.type (engram_kind)",
    );
  }
  if ((args.op === "merge" || args.op === "upsert") && !args.mergeKey) {
    throw new Error(`imprint op='${args.op}' requires mergeKey`);
  }
  const payload: Json = { op: args.op, entry: args.entry };
  if (args.mergeKey !== undefined) payload["merge_key"] = args.mergeKey;
  return createSignal({
    type: SignalType.IMPRINT,
    trace_id: args.traceId,
    parent_id: args.parentId,
    directed: args.directed,
    payload,
    meta: args.meta ?? {},
  });
}

/** [D] Receipt of a completed IMPRINT. `parentId` MUST be the IMPRINT's id. */
export function imprintedSignal(args: {
  traceId: string;
  parentId: string;
  engramId: string;
  op: string;
  id?: string;
  version?: number;
  tookMs?: number;
  error?: string;
  directed?: DirectedInput;
  meta?: Json;
}): Signal {
  const payload: Json = { engram_id: args.engramId, op: args.op };
  if (args.id !== undefined) payload["id"] = args.id;
  if (args.version !== undefined) payload["version"] = args.version;
  if (args.tookMs !== undefined) payload["took_ms"] = args.tookMs;
  if (args.error !== undefined) payload["error"] = args.error;
  return createSignal({
    type: SignalType.IMPRINTED,
    trace_id: args.traceId,
    parent_id: args.parentId,
    directed: args.directed ?? null,
    payload,
    meta: args.meta ?? {},
  });
}
