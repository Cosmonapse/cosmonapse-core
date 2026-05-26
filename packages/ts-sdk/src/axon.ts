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
 * on_refresh. Those scheduling hooks are not part of this port yet.)
 */

import {
  agentOutputSignal,
  clarificationSignal,
  errorSignal,
} from "./signals.js";
import { isClarification, type ContextFetcher, type NeuronFn } from "./neuron.js";
import type { Json, Signal } from "./envelope.js";

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
  private dendrite: unknown = null;

  constructor(opts: AxonOptions) {
    this.neuronId = opts.neuronId;
    this.capabilities = opts.capabilities ?? [];
    this.version = opts.version;
    this.fn = opts.neuronFn;
    this.contextFetcher = opts.contextFetcher ?? noopContextFetcher;
  }

  /** @internal — set by Dendrite.attachAxon. */
  attachTo(dendrite: unknown): void {
    if (this.dendrite !== null && this.dendrite !== dendrite) {
      throw new Error(`Axon '${this.neuronId}' is already attached to a different Dendrite`);
    }
    this.dendrite = dendrite;
  }

  /** @internal */
  detach(): void {
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
