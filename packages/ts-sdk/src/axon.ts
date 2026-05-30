/**
 * @cosmonapse/sdk — axon
 *
 * Agent-side tool that turns a Neuron's raw output into a protocol-valid
 * Signal. Ported from `cosmonapse.axon`.
 *
 * The Axon does NOT touch the Synapse. It owns:
 *   - the Neuron's identity (neuronId, capabilities, version)
 *   - the body of the tool (the NeuronFn)
 *   - response validation:
 *       normal return        -> AGENT_OUTPUT
 *       clarification marker -> CLARIFICATION
 *       thrown error         -> ERROR
 *
 * (The Python Axon also mixes in LifecycleHooks for on_connect / on_schedule /
 * on_refresh. Those scheduling hooks are tracked in PORTING_STATUS.md and are
 * not part of this port yet.)
 */

import {
  agentOutputSignal,
  clarificationSignal,
  errorSignal,
} from "./signals.js";
import { isClarification, type ContextFetcher, type NeuronFn } from "./neuron.js";
import type { Json, Signal } from "./envelope.js";
// Type-only import: erased at runtime under verbatimModuleSyntax, so this does
// NOT introduce a runtime import cycle with dendrite.ts. It restores type
// safety on the back-reference from an Axon to its hosting Dendrite.
import type { Dendrite } from "./dendrite.js";

/**
 * Package-internal keys for the attach/detach handshake. These are deliberately
 * NOT re-exported from index.ts, so only same-package code (the Dendrite) can
 * name them and invoke the methods. This enforces "internal" at the language
 * level — which a `@internal` JSDoc tag on a `public` method does not. External
 * consumers have no way to reference these symbols, so `axon[ATTACH](...)` is
 * effectively private to the package.
 */
export const ATTACH: unique symbol = Symbol("cosmonapse.axon.attach");
export const DETACH: unique symbol = Symbol("cosmonapse.axon.detach");

export interface AxonOptions {
  neuronId: string;
  neuronFn: NeuronFn;
  capabilities?: string[];
  version?: string;
  contextFetcher?: ContextFetcher;
}

const noopContextFetcher: ContextFetcher = () => [];

export class Axon {
  readonly neuronId: string;
  readonly capabilities: string[];
  readonly version: string | undefined;
  private readonly fn: NeuronFn;
  private readonly contextFetcher: ContextFetcher;
  private dendrite: Dendrite | null = null;

  constructor(opts: AxonOptions) {
    this.neuronId = opts.neuronId;
    this.capabilities = opts.capabilities ?? [];
    this.version = opts.version;
    this.fn = opts.neuronFn;
    this.contextFetcher = opts.contextFetcher ?? noopContextFetcher;
  }

  /**
   * Package-internal: invoked by Dendrite.attachAxon via the {@link ATTACH}
   * symbol. Not callable by external consumers (the symbol is not exported from
   * index.ts), so this replaces the previous `@internal`-comment-only contract
   * with real, enforced encapsulation.
   */
  [ATTACH](dendrite: Dendrite): void {
    if (this.dendrite !== null && this.dendrite !== dendrite) {
      throw new Error(`Axon '${this.neuronId}' is already attached to a different Dendrite`);
    }
    this.dendrite = dendrite;
  }

  /** Package-internal: invoked via the {@link DETACH} symbol. */
  [DETACH](): void {
    this.dendrite = null;
  }

  /** Run the Neuron and return AGENT_OUTPUT / CLARIFICATION / ERROR. */
  async handleTask(task: Signal): Promise<Signal> {
    const traceId = task.trace_id;
    const parentId = task.id;
    const input = (task.payload["input"] as Json | undefined) ?? {};
    const contextRef = task.payload["context_ref"] as string | undefined;

    let context: unknown[] = [];
    if (contextRef) {
      try {
        context = await this.contextFetcher(contextRef);
      } catch {
        // Context fetch failures are non-fatal: proceed with empty context.
        context = [];
      }
    }

    let rawOutput: unknown;
    try {
      rawOutput = await this.fn(input, context);
    } catch (err) {
      return errorSignal({
        traceId,
        parentId,
        neuron: this.neuronId,
        code: "NEURON_EXCEPTION",
        message: err instanceof Error ? err.message : String(err),
        recoverable: false,
      });
    }

    if (isClarification(rawOutput)) {
      return clarificationSignal({
        traceId,
        parentId,
        neuron: this.neuronId,
        question: rawOutput.question,
        ...(rawOutput.context !== undefined ? { context: rawOutput.context } : {}),
      });
    }

    const output: Json =
      typeof rawOutput === "object" && rawOutput !== null
        ? (rawOutput as Json)
        : { value: rawOutput };

    return agentOutputSignal({ traceId, parentId, neuron: this.neuronId, output });
  }
}
