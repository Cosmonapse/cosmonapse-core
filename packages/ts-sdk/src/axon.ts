/**
 * @cosmonapse/sdk  -  axon
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
 * Like the Python Axon, this one carries LifecycleHooks (onConnect /
 * onRefresh / onSchedule). The hosting Dendrite drives them: it fires the
 * connect hooks and launches the schedule loops once the Axon is attached and
 * registered, and stops them when the Dendrite stops.
 */

import {
  agentOutputSignal,
  clarificationSignal,
  errorSignal,
  permissionSignal,
} from "./signals.js";
import {
  isClarification,
  isErrorOutput,
  isPermissionRequest,
  type ContextFetcher,
  type NeuronFn,
} from "./neuron.js";
import { neuron, type NeuronSource } from "./neuron-factory.js";
import type { OllamaNeuronOptions, HuggingFaceNeuronOptions } from "./neuron-http.js";
import type { OpenAINeuronOptions, AnthropicNeuronOptions } from "./neuron-openai.js";
import type { McpNeuronOptions } from "./neuron-mcp.js";
import {
  LifecycleHooks,
  type ConnectHook,
  type RefreshHook,
  type ScheduleHook,
} from "./hooks.js";
import type { Json, Signal } from "./envelope.js";
// Type-only import: erased at runtime under verbatimModuleSyntax, so this does
// NOT introduce a runtime import cycle with dendrite.ts. It restores type
// safety on the back-reference from an Axon to its hosting Dendrite.
import type { Dendrite } from "./dendrite.js";

/**
 * Package-internal keys for the attach/detach handshake. These are deliberately
 * NOT re-exported from index.ts, so only same-package code (the Dendrite) can
 * name them and invoke the methods. This enforces "internal" at the language
 * level  -  which a `@internal` JSDoc tag on a `public` method does not. External
 * consumers have no way to reference these symbols, so `axon[ATTACH](...)` is
 * effectively private to the package.
 */
export const ATTACH: unique symbol = Symbol("cosmonapse.axon.attach");
export const DETACH: unique symbol = Symbol("cosmonapse.axon.detach");

/**
 * Recognises a Neuron's *native* output (an LLM's `{ response }`, an MCP
 * server's `{ is_error }`) and normalises it into the marker dict the Axon
 * understands. The recognition the Axon applies before wrapping. May throw to
 * yield an ERROR signal.
 */
export type OutputParser = (raw: unknown) => unknown;

/**
 * A detector registered via `axon.detects*`. Returns the intent's fields on a
 * match, or null/undefined to fall through. May be sync or async.
 */
export type Recogniser = (raw: unknown) => unknown | Promise<unknown>;

export interface AxonOptions {
  neuronId: string;
  neuronFn: NeuronFn;
  capabilities?: string[];
  version?: string;
  contextFetcher?: ContextFetcher;
  /** Recognition the Axon applies to the Neuron's raw output before wrapping. */
  outputParser?: OutputParser;
}

/** Axon metadata accepted by the source-paired factories. */
export interface AxonExtra {
  capabilities?: string[];
  version?: string;
  contextFetcher?: ContextFetcher;
  /** Attach the source's recogniser (default true). */
  recognize?: boolean;
}

const noopContextFetcher: ContextFetcher = () => [];

export class Axon {
  readonly neuronId: string;
  readonly capabilities: string[];
  readonly version: string | undefined;
  private readonly fn: NeuronFn;
  private readonly contextFetcher: ContextFetcher;
  private readonly outputParser: OutputParser | undefined;
  private dendrite: Dendrite | null = null;

  /**
   * Decorator-registered recognisers, one bucket per capability (the asking
   * side; named `detects*` to stay distinct from the Dendrite's `on*` inbound
   * handlers). Applied in precedence error -> clarification -> permission ->
   * output by {@link applyRecognisers}.
   */
  private readonly recognisers: {
    error: Recogniser[];
    clarification: Recogniser[];
    permission: Recogniser[];
    output: Recogniser[];
  } = { error: [], clarification: [], permission: [], output: [] };

  /** @internal  -  lifecycle hooks, driven by the hosting Dendrite. */
  readonly hooks: LifecycleHooks<Axon> = new LifecycleHooks<Axon>(this);

  constructor(opts: AxonOptions) {
    this.neuronId = opts.neuronId;
    this.capabilities = opts.capabilities ?? [];
    this.version = opts.version;
    this.fn = opts.neuronFn;
    this.contextFetcher = opts.contextFetcher ?? noopContextFetcher;
    this.outputParser = opts.outputParser;
  }

  // -- source-paired factories --------------------------------------
  // Build an Axon already paired with one of the `neuron(source, ...)`
  // providers AND wired with the matching recogniser. No new class: the
  // result is a plain Axon.

  private static build(
    neuronId: string,
    neuronFn: NeuronFn,
    source: string,
    extra: AxonExtra,
  ): Axon {
    const recognize = extra.recognize ?? true;
    const o: AxonOptions = { neuronId, neuronFn };
    if (extra.capabilities) o.capabilities = extra.capabilities;
    if (extra.version !== undefined) o.version = extra.version;
    if (extra.contextFetcher) o.contextFetcher = extra.contextFetcher;
    if (recognize) o.outputParser = source === "mcp" ? parseMcpIntents : parseLlmIntents;
    return new Axon(o);
  }

  /** Axon paired with any registered Neuron source + its recogniser. */
  static fromSource(
    source: NeuronSource,
    neuronId: string,
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    opts: any,
    extra: AxonExtra = {},
  ): Axon {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return Axon.build(neuronId, neuron(source as any, opts), source, extra);
  }

  /** Axon paired with the OpenAI Chat Completions API. */
  static openai(neuronId: string, opts: OpenAINeuronOptions, extra: AxonExtra = {}): Axon {
    return Axon.build(neuronId, neuron("openai", opts), "openai", extra);
  }

  /** Axon paired with the Anthropic Messages API. */
  static anthropic(neuronId: string, opts: AnthropicNeuronOptions, extra: AxonExtra = {}): Axon {
    return Axon.build(neuronId, neuron("anthropic", opts), "anthropic", extra);
  }

  /** Axon paired with a local Ollama daemon. */
  static ollama(neuronId: string, opts: OllamaNeuronOptions, extra: AxonExtra = {}): Axon {
    return Axon.build(neuronId, neuron("ollama", opts), "ollama", extra);
  }

  /** Axon paired with a HuggingFace TGI / OpenAI-compatible endpoint. */
  static huggingface(neuronId: string, opts: HuggingFaceNeuronOptions, extra: AxonExtra = {}): Axon {
    return Axon.build(neuronId, neuron("huggingface", opts), "huggingface", extra);
  }

  /** Axon paired with a stdio MCP server. */
  static mcp(neuronId: string, opts: McpNeuronOptions, extra: AxonExtra = {}): Axon {
    return Axon.build(neuronId, neuron("mcp", opts), "mcp", extra);
  }

  // -- recognition decorators ---------------------------------------
  // The asking side: `detects*` registers a detector over the Neuron's raw
  // output, distinct from the Dendrite's `on*` handlers (which consume inbound
  // Signals). Return the intent's fields to match, or null/undefined to fall
  // through. Sync or async; multiple per capability tried in order. These run
  // after `outputParser` and before the literal `__marker__` checks.

  /** Detector returning the AGENT_OUTPUT payload, or null to wrap verbatim. */
  detectsOutput(fn: Recogniser): Recogniser {
    this.recognisers.output.push(fn);
    return fn;
  }
  /** Detector returning `{ question, context? }` to emit CLARIFICATION, or null. */
  detectsClarification(fn: Recogniser): Recogniser {
    this.recognisers.clarification.push(fn);
    return fn;
  }
  /** Detector returning `{ action, scope?, reason?, context? }` for PERMISSION, or null. */
  detectsPermission(fn: Recogniser): Recogniser {
    this.recognisers.permission.push(fn);
    return fn;
  }
  /** Detector returning `{ code?, message?, recoverable? }` to emit ERROR, or null. */
  detectsError(fn: Recogniser): Recogniser {
    this.recognisers.error.push(fn);
    return fn;
  }

  private async applyRecognisers(raw: unknown): Promise<unknown> {
    const rec = this.recognisers;
    if (!rec.error.length && !rec.clarification.length && !rec.permission.length && !rec.output.length) {
      return raw;
    }
    const first = async (fns: Recogniser[]): Promise<unknown> => {
      for (const fn of fns) {
        const r = await fn(raw);
        if (r !== null && r !== undefined) return r;
      }
      return undefined;
    };
    let hit = await first(rec.error);
    if (hit !== undefined) return { __error__: true, ...(hit as object) };
    hit = await first(rec.clarification);
    if (hit !== undefined) return { __clarification__: true, ...(hit as object) };
    hit = await first(rec.permission);
    if (hit !== undefined) return { __permission__: true, ...(hit as object) };
    hit = await first(rec.output);
    if (hit !== undefined) return hit;
    return raw;
  }

  /** Register a fire-once handler called after this Axon connects (attaches + registers). */
  onConnect(fn: ConnectHook<Axon>): ConnectHook<Axon> {
    return this.hooks.onConnect(fn);
  }
  /** Register a handler called whenever this Axon's observable state refreshes. */
  onRefresh(fn: RefreshHook<Axon>): RefreshHook<Axon> {
    return this.hooks.onRefresh(fn);
  }
  /** Register a periodic handler that runs every `everyMs` while the host runs. */
  onSchedule(everyMs: number, fn: ScheduleHook<Axon>): ScheduleHook<Axon> {
    return this.hooks.onSchedule(everyMs, fn);
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
      // Per-source recognition, then decorator-registered recognisers. Inside
      // the try so a recogniser failure surfaces as ERROR, not a crash.
      if (this.outputParser) rawOutput = this.outputParser(rawOutput);
      rawOutput = await this.applyRecognisers(rawOutput);
    } catch (err) {
      return errorSignal({
        traceId,
        parentId,
        directed: { id: this.neuronId },
        code: "NEURON_EXCEPTION",
        message: err instanceof Error ? err.message : String(err),
        recoverable: false,
      });
    }

    // Error marker: a recogniser can request ERROR without throwing.
    if (isErrorOutput(rawOutput)) {
      return errorSignal({
        traceId,
        parentId,
        directed: { id: this.neuronId },
        code: rawOutput.code ?? "NEURON_ERROR",
        message: rawOutput.message ?? "",
        recoverable: Boolean(rawOutput.recoverable),
      });
    }

    if (isClarification(rawOutput)) {
      return clarificationSignal({
        traceId,
        parentId,
        directed: { id: this.neuronId },
        question: rawOutput.question,
        ...(rawOutput.context !== undefined ? { context: rawOutput.context } : {}),
      });
    }

    // Permission marker: same return-and-resume shape as clarification. A
    // Neuron typically tries recall first and only returns this on a miss.
    if (isPermissionRequest(rawOutput)) {
      return permissionSignal({
        traceId,
        parentId,
        directed: { id: this.neuronId },
        action: rawOutput.action,
        ...(rawOutput.scope !== undefined ? { scope: rawOutput.scope } : {}),
        ...(rawOutput.reason !== undefined ? { reason: rawOutput.reason } : {}),
        ...(rawOutput.context !== undefined ? { context: rawOutput.context } : {}),
      });
    }

    const output: Json =
      typeof rawOutput === "object" && rawOutput !== null
        ? (rawOutput as Json)
        : { value: rawOutput };

    return agentOutputSignal({ traceId, parentId, directed: { id: this.neuronId }, output });
  }
}

// ---------------------------------------------------------------------------
// Per-source recognisers (the recognition half the Axon owns)
// ---------------------------------------------------------------------------
// Intent convention (LLM sources): a provider LLM returns free text. To request
// something other than a plain answer it emits one JSON object with a `cosmo`
// key, as the whole response or inside a ```json fenced block:
//   {"cosmo": "clarification", "question": "which region?"}
//   {"cosmo": "permission", "action": "delete", "scope": "/db"}
//   {"cosmo": "error", "code": "REFUSED", "message": "..."}
//   {"cosmo": "output", "output": {"answer": "..."}}
// Anything else (prose, or JSON without `cosmo`) is a normal output.

const INTENT_KEY = "cosmo";
const FENCED_JSON = /```(?:json)?\s*(\{[\s\S]*?\})\s*```/g;

function extractCosmoIntent(text: string): Record<string, unknown> | null {
  if (!text) return null;
  const candidates: string[] = [text.trim()];
  FENCED_JSON.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = FENCED_JSON.exec(text)) !== null) candidates.push(m[1]!);
  for (const cand of candidates) {
    let obj: unknown;
    try {
      obj = JSON.parse(cand);
    } catch {
      continue;
    }
    if (
      obj !== null &&
      typeof obj === "object" &&
      typeof (obj as Record<string, unknown>)[INTENT_KEY] === "string"
    ) {
      return obj as Record<string, unknown>;
    }
  }
  return null;
}

function intentToMarker(intent: Record<string, unknown>): Record<string, unknown> | null {
  const kind = intent[INTENT_KEY];
  if (kind === "clarification") {
    return {
      __clarification__: true,
      question: intent["question"] ?? "",
      ...(intent["context"] !== undefined ? { context: intent["context"] } : {}),
    };
  }
  if (kind === "permission") {
    return {
      __permission__: true,
      action: intent["action"] ?? "",
      ...(intent["scope"] !== undefined ? { scope: intent["scope"] } : {}),
      ...(intent["reason"] !== undefined ? { reason: intent["reason"] } : {}),
      ...(intent["context"] !== undefined ? { context: intent["context"] } : {}),
    };
  }
  if (kind === "error") {
    return {
      __error__: true,
      code: intent["code"] ?? "NEURON_ERROR",
      message: intent["message"] ?? "",
      recoverable: Boolean(intent["recoverable"]),
    };
  }
  if (kind === "output") {
    const out = intent["output"];
    return out !== null && typeof out === "object"
      ? (out as Record<string, unknown>)
      : { value: out };
  }
  return null;
}

/** Recogniser for LLM sources returning `{ response: text, meta }`. */
export function parseLlmIntents(raw: unknown): unknown {
  if (raw === null || typeof raw !== "object") return { value: raw };
  const text = (raw as Record<string, unknown>)["response"];
  if (typeof text === "string") {
    const intent = extractCosmoIntent(text);
    if (intent) {
      const marker = intentToMarker(intent);
      if (marker) return marker;
    }
  }
  return raw;
}

/** Recogniser for the `mcp` source: `is_error` -> ERROR, else pass through. */
export function parseMcpIntents(raw: unknown): unknown {
  if (raw === null || typeof raw !== "object") return { value: raw };
  const r = raw as Record<string, unknown>;
  if (r["is_error"]) {
    const msg = r["response"] ?? r["content"] ?? "MCP tool returned is_error";
    return { __error__: true, code: "MCP_TOOL_ERROR", message: String(msg) };
  }
  const text = r["response"];
  if (typeof text === "string") {
    const intent = extractCosmoIntent(text);
    if (intent) {
      const marker = intentToMarker(intent);
      if (marker) return marker;
    }
  }
  return raw;
}
