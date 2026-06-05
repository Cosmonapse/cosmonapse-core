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
 *   returns  -  a JSON-serialisable object, or a {@link ClarificationOutput}.
 */
export type NeuronFn = (
  input: Json,
  context: unknown[],
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
