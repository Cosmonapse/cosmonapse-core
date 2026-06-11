// The Signal envelope as it arrives over the Prism `/ws` bridge  -  one JSON
// object per WebSocket message. Mirrors ENVELOPE_SPEC.md §4. Consumers must
// ignore unknown fields, so every non-required field is optional here.

export interface Signal {
  v: string;
  id: string;
  trace_id: string;
  parent_id?: string | null;
  type: SignalType;
  directed?: { id: string | null; type: string | null; capabilities: string[] } | null;
  ts: string;
  payload?: Record<string, unknown>;
  meta?: Record<string, unknown>;
}

export type SignalType =
  // ── Core: lifecycle ────────────────────────────────────────────────────
  | "REGISTER"
  | "DEREGISTER"
  | "HEARTBEAT"
  // ── Core: task / workflow ──────────────────────────────────────────────
  | "TASK"
  | "AGENT_OUTPUT"
  | "FINAL"
  | "ERROR"
  | "TASK_OFFER"
  | "BID"
  | "TASK_AWARDED"
  | "TASK_DECLINED"
  // ── Core: cognition ───────────────────────────────────────────────────
  | "THOUGHT_DELTA"
  | "PLAN"
  | "TOOL_CALL"
  | "TOOL_RESULT"
  | "ESCALATION"
  | "CONSENSUS"
  | "CRITIQUE"
  | "DISCOVER"
  // ── Core: clarification / permission ──────────────────────────────────
  | "CLARIFICATION"
  | "CLARIFICATION_ANSWER"
  | "PERMISSION"
  | "PERMISSION_DECISION"
  // ── Engram: memory ────────────────────────────────────────────────────
  | "RECALL"
  | "RECALLED"
  | "IMPRINT"
  | "IMPRINTED"
  // ── Legacy aliases (still emitted by older SDK versions) ──────────────
  | "MEMORY_APPEND"
  | "CONTEXT_SYNC";

export type ParticipantKind = "neuron" | "engram";

/**
 * The single uniform participant-kind check for the whole UI.
 *
 * Reads `payload.role` - the universal discriminator every SDK now emits on
 * REGISTER ("neuron" | "engram"). For envelopes from pre-role SDKs it falls
 * back, on REGISTER only, to the legacy `engram` flag and the `eng_` id
 * convention. Returns `null` for signals that carry no kind evidence (e.g. a
 * TASK or a RECALL) so callers keep whatever kind they already recorded -
 * classification comes from registration, never from traffic.
 */
export function participantKind(sig: Signal): ParticipantKind | null {
  const role = sig.payload?.role;
  if (role === "neuron" || role === "engram") return role;
  if (sig.type === "REGISTER") {
    if (sig.payload?.engram === true) return "engram";
    if (sig.directed?.id?.startsWith("eng_")) return "engram";
    return "neuron";
  }
  return null;
}

// A neuron as Prism accumulates it from the signal stream.
export interface NeuronView {
  id: string;
  count: number;
  kind: ParticipantKind;
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

// Signal types emitted by the Axon/Dendrite side (neuron → synapse).
// Matches cosmonapse.envelope.AXON_TYPES.
export const AXON_TYPES = new Set<SignalType>([
  "AGENT_OUTPUT",
  "CLARIFICATION",
  "PERMISSION",
  "ERROR",
  "REGISTER",
  "DEREGISTER",
  "HEARTBEAT",
  // Engram request side (Axon → Engram backend)
  "RECALL",
  "IMPRINT",
]);

// Signal types targeted at a specific neuron (synapse → neuron).
export const TARGET_TYPES = new Set<SignalType>([
  "TASK",
  "TASK_OFFER",
  "TASK_AWARDED",
  "TASK_DECLINED",
  // Engram reply side (Engram backend → Axon)
  "RECALLED",
  "IMPRINTED",
  // Responses to clarification / permission requests
  "CLARIFICATION_ANSWER",
  "PERMISSION_DECISION",
]);
