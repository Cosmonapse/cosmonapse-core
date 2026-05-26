/**
 * @cosmonapse/sdk — neuron contract
 *
 * A Neuron is a pure function — `(input, context) => output`. It has zero
 * knowledge of the protocol, envelopes, trace IDs, or workflow semantics.
 * Any existing LLM-driven agent can be wrapped to satisfy this signature and
 * become a protocol participant with no modification.
 *
 * (The Python SDK additionally ships provider-backed Neuron factories for
 * Ollama / HuggingFace over httpx. Those HTTP wrappers are not part of this
 * TS port yet — bring your own async function.)
 */

import type { Json } from "./envelope.js";

/**
 * The Neuron function type.
 *   input   — `payload.input` from the TASK envelope (arbitrary JSON).
 *   context — resolved by the Axon from `payload.context_ref` (empty if none).
 *   returns — a JSON-serialisable object, or a {@link ClarificationOutput}.
 */
export type NeuronFn = (
  input: Json,
  context: unknown[],
) => Promise<Json> | Json;

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
