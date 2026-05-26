/**
 * @cosmonapse/sdk — typed signal builders
 *
 * Convenience constructors for the common Signal types. These are NOT required
 * by the protocol — the protocol only requires a valid Signal with the correct
 * `type` and `payload`. They mirror the helpers in `cosmonapse.envelope`.
 */

import { createSignal, newTraceId, SignalType, type Json, type Signal } from "./envelope.js";

/** [C] Dispatch a unit of work to a Neuron. */
export function taskSignal(args: {
  input: Json;
  traceId?: string;
  parentId?: string | null;
  neuron?: string | null;
  contextRef?: string;
  capabilities?: string[];
  meta?: Json;
}): Signal {
  const payload: Json = { input: args.input };
  if (args.contextRef) payload["context_ref"] = args.contextRef;
  if (args.capabilities) payload["capabilities"] = args.capabilities;
  return createSignal({
    type: SignalType.TASK,
    trace_id: args.traceId ?? newTraceId(),
    parent_id: args.parentId ?? null,
    neuron: args.neuron ?? null,
    payload,
    meta: args.meta ?? {},
  });
}

/** [A] Wrap a Neuron's raw output in a neutral AGENT_OUTPUT envelope. */
export function agentOutputSignal(args: {
  traceId: string;
  parentId: string;
  neuron: string;
  output: Json;
  meta?: Json;
}): Signal {
  return createSignal({
    type: SignalType.AGENT_OUTPUT,
    trace_id: args.traceId,
    parent_id: args.parentId,
    neuron: args.neuron,
    payload: { output: args.output },
    meta: args.meta ?? {},
  });
}

/** [A] The Neuron needs more information before it can complete the task. */
export function clarificationSignal(args: {
  traceId: string;
  parentId: string;
  neuron: string;
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
    neuron: args.neuron,
    payload,
    meta: args.meta ?? {},
  });
}

/** [C] Workflow concluded. `result` carries the terminal output. */
export function finalSignal(args: {
  traceId: string;
  parentId: string;
  result: Json;
  neuron?: string | null;
  cost?: Json;
  meta?: Json;
}): Signal {
  const payload: Json = { result: args.result };
  if (args.cost) payload["cost"] = args.cost;
  return createSignal({
    type: SignalType.FINAL,
    trace_id: args.traceId,
    parent_id: args.parentId,
    neuron: args.neuron ?? null,
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
  neuron?: string | null;
  recoverable?: boolean;
  meta?: Json;
}): Signal {
  return createSignal({
    type: SignalType.ERROR,
    trace_id: args.traceId,
    parent_id: args.parentId ?? null,
    neuron: args.neuron ?? null,
    payload: {
      code: args.code,
      message: args.message,
      recoverable: args.recoverable ?? false,
    },
    meta: args.meta ?? {},
  });
}

/** [A] Neuron connecting to the Synapse and declaring its capabilities. */
export function registerSignal(args: {
  neuron: string;
  capabilities: string[];
  version?: string;
  meta?: Json;
}): Signal {
  const payload: Json = { capabilities: args.capabilities };
  if (args.version) payload["version"] = args.version;
  return createSignal({
    type: SignalType.REGISTER,
    trace_id: newTraceId(), // management signals get their own trace
    neuron: args.neuron,
    payload,
    meta: args.meta ?? {},
  });
}

/** [A] Neuron disconnecting from the Synapse. */
export function deregisterSignal(args: {
  neuron: string;
  reason?: string;
  meta?: Json;
}): Signal {
  const payload: Json = {};
  if (args.reason) payload["reason"] = args.reason;
  return createSignal({
    type: SignalType.DEREGISTER,
    trace_id: newTraceId(),
    neuron: args.neuron,
    payload,
    meta: args.meta ?? {},
  });
}

/** [A] Periodic liveness signal from a Neuron. */
export function heartbeatSignal(args: {
  neuron: string;
  status?: string;
  meta?: Json;
}): Signal {
  return createSignal({
    type: SignalType.HEARTBEAT,
    trace_id: newTraceId(),
    neuron: args.neuron,
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
  neuron?: string | null;
  meta?: Json;
}): Signal {
  return createSignal({
    type: SignalType.MEMORY_APPEND,
    trace_id: args.traceId,
    parent_id: args.parentId,
    neuron: args.neuron ?? null,
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
  neuron: string;
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
    neuron: args.neuron,
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
  neuron?: string | null;
  meta?: Json;
}): Signal {
  return createSignal({
    type: SignalType.CRITIQUE,
    trace_id: args.traceId,
    parent_id: args.parentId,
    neuron: args.neuron ?? null,
    payload: {
      target_event_id: args.targetEventId,
      issues: args.issues,
      verdict: args.verdict,
    },
    meta: args.meta ?? {},
  });
}
