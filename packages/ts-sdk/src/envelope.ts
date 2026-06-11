/**
 * @cosmonapse/sdk  -  envelope
 *
 * Signal envelope types and codec. The TypeScript surface mirrors the Python
 * `cosmonapse.envelope` module 1:1 (see ENVELOPE_SPEC.md §7). Every message
 * crossing the Synapse is a Signal  -  a JSON object conforming to this schema.
 *
 * Producer tags (who emits each type):
 *   [A]  Axon (skill/connector)
 *   [C]  Cortex (developer-built orchestrating component)
 */

import { ulid } from "ulid";

// ---------------------------------------------------------------------------
// ULID helpers
// ---------------------------------------------------------------------------

/** Return a prefixed event ULID: `evt_<26-char ULID>`. */
export function newEventId(): string {
  return `evt_${ulid()}`;
}

/** Return a prefixed trace ULID: `trc_<26-char ULID>`. */
export function newTraceId(): string {
  return `trc_${ulid()}`;
}

/** Return a prefixed Engram entry ULID: `eng_<26-char ULID>`. */
export function newEngramId(): string {
  return `eng_${ulid()}`;
}

function nowUtc(): string {
  // RFC 3339 UTC timestamp with millisecond precision.
  return new Date().toISOString();
}

// ---------------------------------------------------------------------------
// Signal types
// ---------------------------------------------------------------------------

export const SignalType = {
  // Lifecycle [A] / [C]
  TASK: "TASK",
  AGENT_OUTPUT: "AGENT_OUTPUT",
  FINAL: "FINAL",
  ERROR: "ERROR",

  // Routing [C]
  TASK_OFFER: "TASK_OFFER",
  BID: "BID",
  TASK_AWARDED: "TASK_AWARDED",
  TASK_DECLINED: "TASK_DECLINED",

  // Cognition [C]
  THOUGHT_DELTA: "THOUGHT_DELTA",
  PLAN: "PLAN",
  TOOL_CALL: "TOOL_CALL",
  TOOL_RESULT: "TOOL_RESULT",

  // Memory [C]
  MEMORY_APPEND: "MEMORY_APPEND",
  ESCALATION: "ESCALATION",

  // Coordination [C] / [A]
  CONSENSUS: "CONSENSUS",
  CONTEXT_SYNC: "CONTEXT_SYNC",
  CRITIQUE: "CRITIQUE",
  CLARIFICATION: "CLARIFICATION",

  // Interactive cognition [A] request / [C] response.
  // CLARIFICATION (above) and PERMISSION are Axon-originated requests a Neuron
  // returns as a marker; the matching *_ANSWER / *_DECISION are emitted by
  // whichever Dendrite answers (a central Cortex or a peer). There is no
  // built-in correlation client - the developer wires the loop (keyed by
  // parent_id == the request's id where needed).
  PERMISSION: "PERMISSION",
  PERMISSION_DECISION: "PERMISSION_DECISION",
  CLARIFICATION_ANSWER: "CLARIFICATION_ANSWER",

  // Agent management [A]
  REGISTER: "REGISTER",
  DEREGISTER: "DEREGISTER",
  HEARTBEAT: "HEARTBEAT",

  // Engram [C]  -  see ENGRAM_DESIGN.md
  RECALL: "RECALL",
  RECALLED: "RECALLED",
  IMPRINT: "IMPRINT",
  IMPRINTED: "IMPRINTED",

  // Discovery [C]
  DISCOVER: "DISCOVER",
} as const;

export type SignalType = (typeof SignalType)[keyof typeof SignalType];

/** Types the Axon (skill) is allowed to produce. */
export const AXON_TYPES: ReadonlySet<SignalType> = new Set<SignalType>([
  SignalType.AGENT_OUTPUT,
  SignalType.CLARIFICATION,
  SignalType.PERMISSION,
  SignalType.ERROR,
  SignalType.REGISTER,
  SignalType.DEREGISTER,
  SignalType.HEARTBEAT,
]);

/** Types the Cortex (orchestrator) is allowed to produce. */
export const SYNAPSE_TYPES: ReadonlySet<SignalType> = new Set<SignalType>([
  SignalType.TASK,
  SignalType.FINAL,
  SignalType.ERROR,
  SignalType.TASK_OFFER,
  SignalType.BID,
  SignalType.TASK_AWARDED,
  SignalType.TASK_DECLINED,
  SignalType.THOUGHT_DELTA,
  SignalType.PLAN,
  SignalType.TOOL_CALL,
  SignalType.TOOL_RESULT,
  SignalType.MEMORY_APPEND,
  SignalType.ESCALATION,
  SignalType.CONSENSUS,
  SignalType.CONTEXT_SYNC,
  SignalType.CRITIQUE,
  // Responses to Axon-originated CLARIFICATION / PERMISSION requests, emitted
  // by the answering Dendrite (central Cortex or peer) and correlated by the
  // requester's CognitionClient via parent_id.
  SignalType.PERMISSION_DECISION,
  SignalType.CLARIFICATION_ANSWER,
  SignalType.DISCOVER,
  SignalType.RECALL,
  SignalType.RECALLED,
  SignalType.IMPRINT,
  SignalType.IMPRINTED,
]);

// ---------------------------------------------------------------------------
// Directed addressing
// ---------------------------------------------------------------------------

/**
 * Unified addressing for a Signal. Mirrors Python `cosmonapse.envelope.Directed`
 * 1:1 so the wire shape is identical across SDKs.
 *
 * A Signal may be addressed three ways, in precedence order:
 * - `id`           Direct address. A `neuron_id` for TASK-family routing, or an
 *                  `engram_id` for RECALL/IMPRINT.
 * - `type`         Type-based routing. A neuron type, or an `engram_kind`.
 * - `capabilities` Capability-based routing.
 *
 * `id` wins over `type`, which wins over `capabilities` on the receiving side.
 */
export interface Directed {
  id: string | null;
  type: string | null;
  capabilities: string[];
}

/** A partial Directed accepted at call sites; missing fields default to null/[]. */
export type DirectedInput = Partial<Directed> | null;

/** Normalise a partial Directed (or null) into a full Directed (or null). */
export function normalizeDirected(d: DirectedInput | undefined): Directed | null {
  if (d === null || d === undefined) return null;
  return {
    id: d.id ?? null,
    type: d.type ?? null,
    capabilities: d.capabilities ? [...d.capabilities] : [],
  };
}

/** Small helper for building a {@link Directed} at call sites. */
export function directedTo(
  id?: string | null,
  opts: { type?: string | null; capabilities?: string[] } = {},
): Directed {
  return {
    id: id ?? null,
    type: opts.type ?? null,
    capabilities: opts.capabilities ? [...opts.capabilities] : [],
  };
}

// ---------------------------------------------------------------------------
// Base envelope
// ---------------------------------------------------------------------------

export type Json = Record<string, unknown>;

/**
 * The universal envelope for every message crossing the Synapse.
 *
 * - `v`         Protocol version. Always `"1"` for this release.
 * - `id`        Unique event ID. Format: `evt_<26-char ULID>`.
 * - `trace_id`  Groups all Signals belonging to one logical workflow (`trc_<ULID>`).
 * - `parent_id` The id of the Signal that caused this one. Optional.
 * - `type`      One of the {@link SignalType} values.
 * - `directed`  Unified addressing (id / type / capabilities). Null when unset.
 * - `ts`        RFC 3339 UTC timestamp of emission.
 * - `payload`   Type-specific content.
 * - `meta`      Non-semantic annotations: model name, token counts, cost, etc.
 */
export interface Signal {
  v: string;
  id: string;
  trace_id: string;
  parent_id: string | null;
  type: SignalType;
  directed: Directed | null;
  ts: string;
  payload: Json;
  meta: Json;
}

export interface NewSignalInput {
  type: SignalType;
  trace_id?: string;
  parent_id?: string | null;
  directed?: DirectedInput;
  payload?: Json;
  meta?: Json;
  v?: string;
  id?: string;
  ts?: string;
}

/** Construct a fully-populated, validated Signal, filling protocol defaults. */
export function createSignal(input: NewSignalInput): Signal {
  const signal: Signal = {
    v: input.v ?? "1",
    id: input.id ?? newEventId(),
    trace_id: input.trace_id ?? newTraceId(),
    parent_id: input.parent_id ?? null,
    type: input.type,
    directed: normalizeDirected(input.directed),
    ts: input.ts ?? nowUtc(),
    payload: input.payload ?? {},
    meta: input.meta ?? {},
  };
  validateSignal(signal);
  return signal;
}

/** Throw if the envelope's identifier fields violate the spec. */
export function validateSignal(signal: Signal): void {
  // Compatibility policy (ENVELOPE_SPEC §8/§9): same-major envelopes are
  // accepted ("1" or "1.x"; unknown payload/meta fields must be ignored by
  // consumers); a different major version is rejected at decode time.
  const major = signal.v.split(".", 1)[0];
  if (major !== "1") {
    throw new Error(
      `unsupported protocol version '${signal.v}': this SDK speaks major ` +
        `version 1 (accepts '1' or '1.x')`,
    );
  }
  if (!signal.id.startsWith("evt_")) {
    throw new Error(`Signal id must start with 'evt_', got: ${signal.id}`);
  }
  if (!signal.trace_id.startsWith("trc_")) {
    throw new Error(`trace_id must start with 'trc_', got: ${signal.trace_id}`);
  }
  if (signal.parent_id !== null && !signal.parent_id.startsWith("evt_")) {
    throw new Error(`parent_id must start with 'evt_', got: ${signal.parent_id}`);
  }
}

/** Serialise to UTF-8 JSON bytes for wire transmission. */
export function encode(signal: Signal): Uint8Array {
  return new TextEncoder().encode(JSON.stringify(signal));
}

/** Deserialise from JSON bytes or string into a validated Signal. */
export function decode(data: Uint8Array | string): Signal {
  const text = typeof data === "string" ? data : new TextDecoder().decode(data);
  const parsed = JSON.parse(text) as Partial<Signal>;
  if (parsed.type === undefined) {
    throw new Error("Signal is missing required field 'type'");
  }
  return createSignal(parsed as NewSignalInput);
}

/**
 * Construct a reply Signal that shares `source`'s trace_id and sets
 * `parent_id` to `source`'s id.
 */
export function reply(
  source: Signal,
  opts: {
    type: SignalType;
    payload?: Json;
    directed?: DirectedInput;
    meta?: Json;
  },
): Signal {
  return createSignal({
    type: opts.type,
    trace_id: source.trace_id,
    parent_id: source.id,
    payload: opts.payload ?? {},
    directed: opts.directed !== undefined ? opts.directed : source.directed,
    meta: opts.meta ?? {},
  });
}
