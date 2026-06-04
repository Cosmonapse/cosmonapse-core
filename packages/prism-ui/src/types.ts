// The Signal envelope as it arrives over the Prism `/ws` bridge — one JSON
// object per WebSocket message. Mirrors ENVELOPE_SPEC.md §4. Consumers must
// ignore unknown fields, so every non-required field is optional here.

export interface Signal {
  v: string;
  id: string;
  trace_id: string;
  parent_id?: string | null;
  type: SignalType;
  neuron?: string | null;
  ts: string;
  payload?: Record<string, unknown>;
  meta?: Record<string, unknown>;
}

export type SignalType =
  | "TASK"
  | "AGENT_OUTPUT"
  | "FINAL"
  | "ERROR"
  | "CLARIFICATION"
  | "REGISTER"
  | "DEREGISTER"
  | "HEARTBEAT"
  | "TASK_OFFER"
  | "BID"
  | "TASK_AWARDED"
  | "TASK_DECLINED"
  | "THOUGHT_DELTA"
  | "PLAN"
  | "TOOL_CALL"
  | "TOOL_RESULT"
  | "MEMORY_APPEND"
  | "ESCALATION"
  | "CONSENSUS"
  | "CONTEXT_SYNC"
  | "CRITIQUE"
  | "DISCOVER";

// A neuron as Prism accumulates it from the signal stream.
export interface NeuronView {
  id: string;
  count: number;
  capabilities: string[];
  version?: string;
  firstSeen: string;
  lastType?: SignalType;
  lastTs?: string;
  deregistered?: boolean;
}

// Prism-side control/error envelopes carry meta.source === "prism".
export function isPrismError(sig: Signal): boolean {
  return sig.type === "ERROR" && sig.meta?.source === "prism";
}

export const SYNAPSE_NODE = "__synapse__";

// Signal types whose flow is neuron → synapse (Axon/Dendrite emitted).
export const AXON_TYPES = new Set<SignalType>([
  "AGENT_OUTPUT",
  "CLARIFICATION",
  "ERROR",
  "REGISTER",
  "DEREGISTER",
  "HEARTBEAT",
  "BID",
]);

// Signal types whose flow is synapse → neuron (targeted at a consumer).
export const TARGET_TYPES = new Set<SignalType>([
  "TASK",
  "TASK_OFFER",
  "TASK_AWARDED",
  "TASK_DECLINED",
]);
