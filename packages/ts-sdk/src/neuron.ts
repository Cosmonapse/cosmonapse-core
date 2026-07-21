/**
 * @cosmonapse/sdk  -  neuron contract
 *
 * A Neuron is a pure function  -  `(input, context) => output`. It has zero
 * knowledge of the protocol, envelopes, trace IDs, or workflow semantics.
 * Any existing LLM-driven agent can be wrapped to satisfy this signature and
 * become a protocol participant with no modification.
 *
 * Provider-backed Neuron factories (Ollama / HuggingFace over `fetch`) are
 * available via `ollamaNeuron` / `huggingFaceNeuron` (see neuron-http.ts) or the
 * unified `neuron("ollama" | "huggingface", …)` factory  -  or bring your own
 * async function that satisfies this signature.
 */

import type { Json } from "./envelope.js";

/**
 * The Neuron function type.
 *   input    -  `payload.input` from the TASK envelope (arbitrary JSON).
 *   context  -  resolved by the Axon from `payload.context_ref` (empty if none).
 *   returns  -  a JSON-serialisable object, a {@link ClarificationOutput}, or a
 *               {@link PermissionRequestOutput}.
 */
/**
 * Engram helpers handed to a Neuron as its (optional) third argument when the
 * hosting Axon declares `engrams: [...]` bindings. The TS counterpart to the
 * Python SDK's parameter-introspected `recall=` / `imprint=` injection  -  TS
 * cannot introspect parameter names, so the helpers ride a context object the
 * Neuron is free to ignore.
 */
export interface NeuronHelpers {
  recall(
    name: string,
    args: {
      query: Record<string, unknown>;
      filters?: Record<string, unknown>;
      contextRef?: string;
      deadlineMs?: number;
      recallMode?: "first" | "merge" | "all";
      minConfidence?: number;
      meta?: Record<string, unknown>;
    },
  ): Promise<unknown>;
  imprint(
    name: string,
    args: {
      op: "add" | "append" | "merge" | "upsert" | "delete";
      entry: Record<string, unknown>;
      mergeKey?: string;
      awaitAck?: boolean;
      deadlineMs?: number;
      meta?: Record<string, unknown>;
    },
  ): Promise<unknown>;
  /** Dispatch one tool call through a declared Effector binding (`name`) and
   *  await the ToolOutcome. Throws EffectorNotBound for undeclared names. */
  callTool(
    name: string,
    args: {
      tool: string;
      args?: Record<string, unknown>;
      callId?: string;
      deadlineMs?: number;
      meta?: Record<string, unknown>;
    },
  ): Promise<unknown>;
}

export type NeuronFn = (
  input: Json,
  context: unknown[],
  helpers?: NeuronHelpers,
) => Promise<Json> | Json;

/**
 * A NeuronFn that also exposes an async `close()` to release any resource it
 * holds (e.g. an MCP subprocess). The Axon calls `close()` automatically when
 * it deregisters.
 */
export interface CloseableNeuronFn extends NeuronFn {
  close(): Promise<void>;
}

/** Async fetcher the Axon uses to resolve a `context_ref` into context items. */
export type ContextFetcher = (ref: string) => Promise<unknown[]> | unknown[];

/**
 * Marker a Neuron returns to request more information instead of producing a
 * result. The Axon converts this into a CLARIFICATION signal.
 */
export interface ClarificationOutput {
  __clarification__: true;
  question: string;
  context?: Json;
}

/** Build a clarification result for a Neuron to return. */
export function clarify(question: string, context?: Json): ClarificationOutput {
  return context === undefined
    ? { __clarification__: true, question }
    : { __clarification__: true, question, context };
}

/** Type guard: did the Neuron return a clarification marker? */
export function isClarification(output: unknown): output is ClarificationOutput {
  return (
    typeof output === "object" &&
    output !== null &&
    (output as Record<string, unknown>)["__clarification__"] === true
  );
}

/**
 * Marker a Neuron returns to request permission to perform `action` instead of
 * producing a result. The Axon converts this into a PERMISSION signal. Same
 * return-and-resume shape as {@link ClarificationOutput}: the Neuron typically
 * tries `recall(...)` against an Engram first and only returns this on a miss.
 */
export interface PermissionRequestOutput {
  __permission__: true;
  action: string;
  scope?: Json;
  reason?: string;
  context?: Json;
}

/** Build a permission-request result for a Neuron to return. */
export function permissionRequest(
  action: string,
  opts: { scope?: Json; reason?: string; context?: Json } = {},
): PermissionRequestOutput {
  return {
    __permission__: true,
    action,
    ...(opts.scope !== undefined ? { scope: opts.scope } : {}),
    ...(opts.reason !== undefined ? { reason: opts.reason } : {}),
    ...(opts.context !== undefined ? { context: opts.context } : {}),
  };
}

/** Type guard: did the Neuron return a permission-request marker? */
export function isPermissionRequest(output: unknown): output is PermissionRequestOutput {
  return (
    typeof output === "object" &&
    output !== null &&
    (output as Record<string, unknown>)["__permission__"] === true
  );
}

/**
 * Marker that yields an ERROR signal without throwing. A recogniser (e.g. the
 * MCP one on `is_error`) returns this so the Axon emits ERROR with a supplied
 * code/message instead of a generic NEURON_EXCEPTION.
 */
export interface ErrorOutput {
  __error__: true;
  code?: string;
  message?: string;
  recoverable?: boolean;
}

/** Build an error result for a Neuron / recogniser to return. */
export function errorResult(
  message: string,
  opts: { code?: string; recoverable?: boolean } = {},
): ErrorOutput {
  return {
    __error__: true,
    message,
    code: opts.code ?? "NEURON_ERROR",
    recoverable: opts.recoverable ?? false,
  };
}

/** Type guard: did the Neuron / recogniser return an error marker? */
export function isErrorOutput(output: unknown): output is ErrorOutput {
  return (
    typeof output === "object" &&
    output !== null &&
    (output as Record<string, unknown>)["__error__"] === true
  );
}
