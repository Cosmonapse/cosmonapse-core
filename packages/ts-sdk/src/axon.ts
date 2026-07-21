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
 * Host-side behaviour (the standard wiring pattern): `axon.host.on*(fn,
 * filter?)` queues a Dendrite signal-handler registration at module scope;
 * the hosting Dendrite replays it right after emitting REGISTER for this
 * Axon (before the onConnect hooks fire) and ensures the inbound
 * subscription. This replaces hand-written onConnect wiring.
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
  type NeuronHelpers,
} from "./neuron.js";
import { EngramNotBound, type EngramBinding } from "./engram.js";
import {
  EffectorError,
  EffectorNotBound,
  type EffectorBinding,
  type ToolOutcome,
} from "./effector.js";
import {
  TOOL_STANDARDS,
  extractToolCall,
  type NativeToolCall,
} from "./effector-standards.js";
import { runWithTraceContext } from "./trace-context.js";
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
import { SignalType, type Json, type Signal } from "./envelope.js";
// Type-only import: erased at runtime under verbatimModuleSyntax, so this does
// NOT introduce a runtime import cycle with dendrite.ts. It restores type
// safety on the back-reference from an Axon to its hosting Dendrite.
import type { Dendrite, HandlerFilter, SignalHandler } from "./dendrite.js";

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
/** @internal Applies queued `axon.host` registrations to the hosting Dendrite. */
export const APPLY_HOST: unique symbol = Symbol("cosmonapse.axon.applyHost");

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

/**
 * Deadline applied to a native tool call dispatched by the Axon when the
 * matched EffectorBinding declares no `defaultDeadlineMs` of its own. A tool
 * call must not hang the TASK forever.
 */
export const DEFAULT_TOOL_DEADLINE_MS = 30_000;

export interface AxonOptions {
  neuronId: string;
  neuronFn: NeuronFn;
  capabilities?: string[];
  version?: string;
  /** Participant kind carried on REGISTER as `directed.type` - the Neuron-side
   *  analogue of an Engram's `engram_kind`. Defaults to `"neuron"`. */
  neuronKind?: string;
  contextFetcher?: ContextFetcher;
  /** Recognition the Axon applies to the Neuron's raw output before wrapping. */
  outputParser?: OutputParser;
  /**
   * Engram bindings the Neuron may address. Keyed by `binding.name`  -  the
   * Neuron passes that name to `helpers.recall(...)` / `helpers.imprint(...)`
   * (the helpers object is the Neuron's optional third argument). The Axon
   * enforces the whitelist, so a Neuron cannot hit an Engram it was not
   * declared to depend on.
   */
  engrams?: EngramBinding[];
  /**
   * Effector bindings the Neuron may act through. Requires `toolStandard` -
   * without a declared dialect the Axon cannot recognise a tool call in the
   * raw output, so the bindings would be dead wiring (construction throws).
   */
  effectors?: EffectorBinding[];
  /**
   * The *native* tool-call dialect this Axon's Neuron emits ("hermes" |
   * "claude" | "codex"). When set, the Axon runs the dialect's parser over
   * the raw output before the cosmo parser / recognisers; a match is
   * translated and (with a serving binding) dispatched through the
   * EffectorClient, the observation riding the AGENT_OUTPUT. `toolStandard`
   * alone (no bindings) is legal: pure translation, dispatch left to the
   * host chain.
   */
  toolStandard?: string;
}

/** Axon metadata accepted by the source-paired factories. */
export interface AxonExtra {
  capabilities?: string[];
  version?: string;
  /** Participant kind carried on REGISTER as `directed.type` (default "neuron"). */
  neuronKind?: string;
  contextFetcher?: ContextFetcher;
  /** Attach the source's recogniser (default true). */
  recognize?: boolean;
  /**
   * Append {@link COSMO_INTENT_SYSTEM_PROMPT} to the source's `system` prompt
   * so the model knows the `{"cosmo": ...}` convention the recogniser parses.
   * Default: true exactly when `recognize` is on and the source accepts a
   * `system` option (every LLM source except `huggingface`; `mcp` is never
   * taught). Pass false to opt out; passing true for an unsupported source
   * throws.
   */
  teachIntents?: boolean;
  /** Effector bindings the Neuron may act through (requires `toolStandard`). */
  effectors?: EffectorBinding[];
  /** Native tool-call dialect the Neuron emits ("hermes" | "claude" | "codex"). */
  toolStandard?: string;
}

const noopContextFetcher: ContextFetcher = () => [];

/** One queued `axon.host` registration, replayed onto the hosting Dendrite. */
interface HostRegistration {
  type: SignalType;
  fn: SignalHandler;
  filter?: HandlerFilter;
}

/**
 * Deferred Dendrite signal decorators, declared on the Axon.
 *
 * `axon.host.onToolCall(fn, { neuron: "websearch" })` queues a handler at
 * module scope; the Axon replays it onto the **hosting Dendrite** right
 * after that Dendrite emits REGISTER for it (before `axon.onConnect` hooks
 * fire) and ensures the inbound subscription. THE standard way to declare
 * host-side behaviour (chain handlers, tool servers) in a Neuron's module -
 * no hand-written onConnect wiring:
 *
 * ```ts
 * AXON.host.onAgentOutput(async (sig) => { ... }, { neuron: "planner" });
 * AXON.host.onToolCall(async (sig) => { ... }, { neuron: "websearch" });
 * ```
 *
 * `onSignal` is the generic escape hatch for any SignalType; the named
 * helpers mirror the Dendrite's cognition / reply surface.
 */
export class AxonHost {
  constructor(private readonly regs: HostRegistration[]) {}

  /** Generic form - queue a handler for any SignalType. */
  onSignal(type: SignalType, fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    this.regs.push(filter === undefined ? { type, fn } : { type, fn, filter });
    return fn;
  }

  // -- reply / lifecycle --
  onAgentOutput(fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    return this.onSignal(SignalType.AGENT_OUTPUT, fn, filter);
  }
  onFinal(fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    return this.onSignal(SignalType.FINAL, fn, filter);
  }
  onErrorSignal(fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    return this.onSignal(SignalType.ERROR, fn, filter);
  }
  onClarification(fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    return this.onSignal(SignalType.CLARIFICATION, fn, filter);
  }
  onPermission(fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    return this.onSignal(SignalType.PERMISSION, fn, filter);
  }

  // -- cognition --
  onPlan(fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    return this.onSignal(SignalType.PLAN, fn, filter);
  }
  onThoughtDelta(fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    return this.onSignal(SignalType.THOUGHT_DELTA, fn, filter);
  }
  onToolCall(fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    return this.onSignal(SignalType.TOOL_CALL, fn, filter);
  }
  onToolResult(fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    return this.onSignal(SignalType.TOOL_RESULT, fn, filter);
  }
  onMemoryAppend(fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    return this.onSignal(SignalType.MEMORY_APPEND, fn, filter);
  }
  onCritique(fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    return this.onSignal(SignalType.CRITIQUE, fn, filter);
  }
  onEscalation(fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    return this.onSignal(SignalType.ESCALATION, fn, filter);
  }
  onConsensus(fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    return this.onSignal(SignalType.CONSENSUS, fn, filter);
  }
  onContextSync(fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    return this.onSignal(SignalType.CONTEXT_SYNC, fn, filter);
  }
}

export class Axon {
  readonly neuronId: string;
  readonly capabilities: string[];
  readonly version: string | undefined;
  readonly neuronKind: string;
  private readonly fn: NeuronFn;
  private readonly contextFetcher: ContextFetcher;
  private readonly outputParser: OutputParser | undefined;
  private readonly engramBindings = new Map<string, EngramBinding>();
  private readonly effectorBindings = new Map<string, EffectorBinding>();
  private readonly _toolStandard: string | null;
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

  /** Pre-task hooks (beforeTask): transform/validate/reject the TASK input
   *  before the Neuron runs. */
  private readonly beforeTaskHooks: Array<(input: Json) => unknown | Promise<unknown>> = [];

  /** @internal  -  lifecycle hooks, driven by the hosting Dendrite. */
  readonly hooks: LifecycleHooks<Axon> = new LifecycleHooks<Axon>(this);

  /** Deferred host-side registrations, replayed at REGISTER time. */
  private readonly hostRegs: HostRegistration[] = [];
  private hostRegsApplied = false;
  /** Deferred Dendrite decorators - see {@link AxonHost}. */
  readonly host: AxonHost = new AxonHost(this.hostRegs);

  constructor(opts: AxonOptions) {
    this.neuronId = opts.neuronId;
    this.capabilities = opts.capabilities ?? [];
    this.version = opts.version;
    this.neuronKind = opts.neuronKind ?? "neuron";
    this.fn = opts.neuronFn;
    this.contextFetcher = opts.contextFetcher ?? noopContextFetcher;
    this.outputParser = opts.outputParser;
    for (const b of opts.engrams ?? []) {
      if (this.engramBindings.has(b.name)) {
        throw new Error(`Axon '${opts.neuronId}': duplicate EngramBinding name '${b.name}'`);
      }
      this.engramBindings.set(b.name, b);
    }

    // Effector bindings - the tools the Neuron may act through. THE RULE: an
    // Axon may hold EffectorBindings only when tool calls are enabled via
    // toolStandard, naming the native dialect its Neuron emits ("hermes" |
    // "claude" | "codex"). Without a standard the Axon cannot recognise a
    // call in the raw output, so the bindings would be dead wiring - fail at
    // construction, not silently at runtime. toolStandard alone (no bindings)
    // is legal: pure translation, dispatch left to the host chain.
    let toolStandard: string | null = null;
    if (opts.toolStandard !== undefined) {
      const std = opts.toolStandard.toLowerCase();
      if (!(std in TOOL_STANDARDS)) {
        throw new Error(
          `Axon '${opts.neuronId}': unknown toolStandard '${opts.toolStandard}'; ` +
            `supported: ${Object.keys(TOOL_STANDARDS).sort().join(", ")}`,
        );
      }
      toolStandard = std;
    }
    if (opts.effectors?.length && toolStandard === null) {
      throw new Error(
        `Axon '${opts.neuronId}': effectors requires toolStandard (one of ` +
          `${Object.keys(TOOL_STANDARDS).sort().join(", ")}) so the Axon can ` +
          "recognise the Neuron's native tool calls",
      );
    }
    this._toolStandard = toolStandard;
    for (const eb of opts.effectors ?? []) {
      if (this.effectorBindings.has(eb.name)) {
        throw new Error(`Axon '${opts.neuronId}': duplicate EffectorBinding name '${eb.name}'`);
      }
      this.effectorBindings.set(eb.name, eb);
    }
  }

  /** Declared Engram bindings, keyed by name. */
  get engrams(): ReadonlyMap<string, EngramBinding> {
    return new Map(this.engramBindings);
  }

  /** Declared Effector bindings, keyed by name. */
  get effectors(): ReadonlyMap<string, EffectorBinding> {
    return new Map(this.effectorBindings);
  }

  /** The native tool-call dialect this Axon recognises, or null. */
  get toolStandard(): string | null {
    return this._toolStandard;
  }

  private resolveEffectorBinding(name: string): EffectorBinding {
    const binding = this.effectorBindings.get(name);
    if (!binding) {
      throw new EffectorNotBound(
        `Axon '${this.neuronId}': no Effector binding named '${name}'; ` +
          `available: ${[...this.effectorBindings.keys()].sort().join(", ")}`,
      );
    }
    return binding;
  }

  /** Which binding serves `tool`? (1) a binding whose `tools` lists it, (2) a
   *  binding named after it, (3) the only binding when exactly one is
   *  declared. Null on no match - never a guess between several. */
  private resolveBindingForTool(tool: string): EffectorBinding | null {
    for (const b of this.effectorBindings.values()) {
      if (b.tools && b.tools.includes(tool)) return b;
    }
    const named = this.effectorBindings.get(tool);
    if (named) return named;
    if (this.effectorBindings.size === 1) {
      return [...this.effectorBindings.values()][0]!;
    }
    return null;
  }

  private resolveBinding(name: string): EngramBinding {
    const binding = this.engramBindings.get(name);
    if (!binding) {
      throw new EngramNotBound(
        `Axon '${this.neuronId}': no Engram binding named '${name}'; ` +
          `available: ${[...this.engramBindings.keys()].sort().join(", ")}`,
      );
    }
    return binding;
  }

  /** Build the per-task helpers object handed to the Neuron as its third
   *  argument. Helpers throw EngramNotBound for undeclared names and
   *  require a hosting Dendrite (the only thing the Axon pulls from it). */
  private buildHelpers(traceId: string, parentId: string): NeuronHelpers {
    const requireClient = (): import("./engram-client.js").EngramClient => {
      if (this.dendrite === null) {
        throw new Error(
          `Axon '${this.neuronId}': not attached to a Dendrite; engram ` +
            "helpers require a hosting Dendrite",
        );
      }
      return this.dendrite.engramClient;
    };
    const requireEffectorClient = (): import("./effector-client.js").EffectorClient => {
      if (this.dendrite === null) {
        throw new Error(
          `Axon '${this.neuronId}': not attached to a Dendrite; effector ` +
            "helpers require a hosting Dendrite",
        );
      }
      return this.dendrite.effectorClient;
    };
    return {
      recall: async (name, args) => {
        const binding = this.resolveBinding(name);
        return requireClient().recall({
          binding,
          query: args.query,
          traceId,
          parentId,
          ...(args.filters !== undefined ? { filters: args.filters } : {}),
          ...(args.contextRef !== undefined ? { contextRef: args.contextRef } : {}),
          ...(args.deadlineMs !== undefined ? { deadlineMs: args.deadlineMs } : {}),
          ...(args.recallMode !== undefined ? { recallMode: args.recallMode } : {}),
          ...(args.minConfidence !== undefined ? { minConfidence: args.minConfidence } : {}),
          ...(args.meta !== undefined ? { meta: args.meta } : {}),
        });
      },
      imprint: async (name, args) => {
        const binding = this.resolveBinding(name);
        return requireClient().imprint({
          binding,
          op: args.op,
          entry: args.entry,
          traceId,
          parentId,
          ...(args.mergeKey !== undefined ? { mergeKey: args.mergeKey } : {}),
          ...(args.awaitAck !== undefined ? { awaitAck: args.awaitAck } : {}),
          ...(args.deadlineMs !== undefined ? { deadlineMs: args.deadlineMs } : {}),
          ...(args.meta !== undefined ? { meta: args.meta } : {}),
        });
      },
      callTool: async (name, args) => {
        const binding = this.resolveEffectorBinding(name);
        return requireEffectorClient().call({
          binding,
          tool: args.tool,
          traceId,
          parentId,
          neuron: this.neuronId,
          ...(args.args !== undefined ? { args: args.args } : {}),
          ...(args.callId !== undefined ? { callId: args.callId } : {}),
          ...(args.deadlineMs !== undefined ? { deadlineMs: args.deadlineMs } : {}),
          ...(args.meta !== undefined ? { meta: args.meta } : {}),
        });
      },
    };
  }

  // -- source-paired factories --------------------------------------
  // Build an Axon already paired with one of the `neuron(source, ...)`
  // providers AND wired with the matching recogniser. No new class: the
  // result is a plain Axon.

  /** Resolve the teach-intents decision and return (possibly augmented) source opts. */
  private static applyTeachIntents(
    source: string,
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    opts: any,
    extra: AxonExtra,
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
  ): any {
    const recognize = extra.recognize ?? true;
    const teach =
      extra.teachIntents ?? (recognize && SYSTEM_CAPABLE_SOURCES.has(source.toLowerCase()));
    if (!teach) return opts;
    if (!SYSTEM_CAPABLE_SOURCES.has(source.toLowerCase())) {
      throw new Error(
        `teachIntents: true is not supported for source '${source}': its ` +
          "Neuron wrapper accepts no system option. Embed the convention in " +
          "the prompt yourself (COSMO_INTENT_SYSTEM_PROMPT).",
      );
    }
    const existing = (opts as { system?: string } | undefined)?.system;
    return {
      ...(opts ?? {}),
      system: existing
        ? `${existing}\n\n${COSMO_INTENT_SYSTEM_PROMPT}`
        : COSMO_INTENT_SYSTEM_PROMPT,
    };
  }

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
    if (extra.neuronKind !== undefined) o.neuronKind = extra.neuronKind;
    if (extra.contextFetcher) o.contextFetcher = extra.contextFetcher;
    if (extra.effectors) o.effectors = extra.effectors;
    if (extra.toolStandard !== undefined) o.toolStandard = extra.toolStandard;
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
    const o = Axon.applyTeachIntents(source, opts, extra);
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return Axon.build(neuronId, neuron(source as any, o), source, extra);
  }

  /** Axon paired with the OpenAI Chat Completions API. */
  static openai(neuronId: string, opts: OpenAINeuronOptions, extra: AxonExtra = {}): Axon {
    const o = Axon.applyTeachIntents("openai", opts, extra) as OpenAINeuronOptions;
    return Axon.build(neuronId, neuron("openai", o), "openai", extra);
  }

  /** Axon paired with the Anthropic Messages API. */
  static anthropic(neuronId: string, opts: AnthropicNeuronOptions, extra: AxonExtra = {}): Axon {
    const o = Axon.applyTeachIntents("anthropic", opts, extra) as AnthropicNeuronOptions;
    return Axon.build(neuronId, neuron("anthropic", o), "anthropic", extra);
  }

  /** Axon paired with a local Ollama daemon. */
  static ollama(neuronId: string, opts: OllamaNeuronOptions, extra: AxonExtra = {}): Axon {
    const o = Axon.applyTeachIntents("ollama", opts, extra) as OllamaNeuronOptions;
    return Axon.build(neuronId, neuron("ollama", o), "ollama", extra);
  }

  /** Axon paired with a HuggingFace TGI / OpenAI-compatible endpoint. */
  static huggingface(neuronId: string, opts: HuggingFaceNeuronOptions, extra: AxonExtra = {}): Axon {
    const o = Axon.applyTeachIntents("huggingface", opts, extra) as HuggingFaceNeuronOptions;
    return Axon.build(neuronId, neuron("huggingface", o), "huggingface", extra);
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

  /**
   * Register a pre-task hook over the TASK's `input`. Runs before the Neuron.
   * Sync or async; multiple hooks run in registration order, each receiving
   * the previous one's result. Return a (new) object to replace the input,
   * return null/undefined to pass through unchanged, or throw to reject the
   * TASK (surfaces as an ERROR Signal, code NEURON_EXCEPTION). The natural
   * place for input normalisation or per-Axon policy checks.
   */
  beforeTask(fn: (input: Json) => unknown | Promise<unknown>): (input: Json) => unknown | Promise<unknown> {
    this.beforeTaskHooks.push(fn);
    return fn;
  }

  private async applyBeforeTask(input: Json): Promise<Json> {
    let current = input;
    for (const fn of this.beforeTaskHooks) {
      const r = await fn(current);
      if (r !== null && r !== undefined) current = r as Json;
    }
    return current;
  }

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

  /**
   * Package-internal: invoked by the hosting Dendrite (via {@link APPLY_HOST})
   * right after it emits REGISTER for this Axon and before the onConnect
   * hooks fire. Replays every `axon.host` registration onto the Dendrite and
   * ensures the inbound subscriptions. Applied exactly once per Axon.
   */
  async [APPLY_HOST](dendrite: Dendrite): Promise<void> {
    if (this.hostRegsApplied || this.hostRegs.length === 0) return;
    this.hostRegsApplied = true;
    const types = new Set<SignalType>();
    for (const { type, fn, filter } of this.hostRegs) {
      dendrite.onSignal(type, fn, filter);
      types.add(type);
    }
    await dendrite.ensureSubscribed(...types);
  }

  /** Run the Neuron and return AGENT_OUTPUT / CLARIFICATION / ERROR.
   *
   * Binds the TASK's (traceId, parentId=task.id) as the ambient trace
   * context for the whole handling pass  -  neuronFn, detectors, and hooks
   * included  -  so engram calls made without explicit trace plumbing (e.g.
   * `dendrite.imprint` from a `detectsOutput` hook) are attributed to this
   * task's trace. */
  async handleTask(task: Signal): Promise<Signal> {
    return runWithTraceContext(task.trace_id, task.id, () =>
      this.handleTaskInner(task),
    );
  }

  private async handleTaskInner(task: Signal): Promise<Signal> {
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
    let nativeCall: NativeToolCall | null = null;
    try {
      const effectiveInput = this.beforeTaskHooks.length
        ? await this.applyBeforeTask(input)
        : input;
      // Helpers ride as an optional third argument the Neuron may ignore  -
      // only built when bindings are declared, so a misaddressed name fails
      // loudly with EngramNotBound rather than silently no-opping.
      const helpers = this.engramBindings.size || this.effectorBindings.size
        ? this.buildHelpers(traceId, parentId)
        : undefined;
      rawOutput = await this.fn(effectiveInput, context, helpers);
      // Native tool-call recognition (toolStandard). The model spoke its
      // trained dialect; a match takes precedence over the cosmo parser and
      // recognisers - a tool call IS the intent, there is nothing further to
      // recognise.
      if (this._toolStandard !== null) {
        nativeCall = extractToolCall(rawOutput, this._toolStandard);
      }
      if (nativeCall === null) {
        // Per-source recognition, then decorator-registered recognisers.
        // Inside the try so a recogniser failure surfaces as ERROR, not a
        // crash.
        if (this.outputParser) rawOutput = this.outputParser(rawOutput);
        rawOutput = await this.applyRecognisers(rawOutput);
      }
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

    // Native tool call: translate-and-act. With a serving binding the Axon
    // dispatches through the EffectorClient and the AGENT_OUTPUT carries the
    // observation; with no bindings at all it carries the translated call for
    // the host chain to execute (pure translation).
    if (nativeCall !== null) {
      return this.dispatchNativeToolCall(nativeCall, traceId, parentId);
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

  /**
   * Act on a recognised native tool call and wrap the observation.
   *
   * Always returns AGENT_OUTPUT: a tool failure (timeout, tool-level error,
   * no serving binding) rides `error` in the output payload for the
   * Neuron/host to react to - it never terminates the TASK. With no bindings
   * declared the translated call passes through unexecuted
   * (`{ tool, args, call_id }`) for the host chain to run - the pre-binding
   * harness pattern, minus the hand-written parser.
   */
  private async dispatchNativeToolCall(
    call: NativeToolCall,
    traceId: string,
    parentId: string,
  ): Promise<Signal> {
    const out: Json = { tool: call.tool, args: call.args };
    if (call.callId !== null) out["call_id"] = call.callId;
    const wrap = (): Signal =>
      agentOutputSignal({ traceId, parentId, directed: { id: this.neuronId }, output: out });

    if (this.effectorBindings.size === 0) return wrap();

    const binding = this.resolveBindingForTool(call.tool);
    if (binding === null) {
      out["error"] =
        `no effector binding serves tool '${call.tool}'; bound: ` +
        [...this.effectorBindings.keys()].sort().join(", ");
      return wrap();
    }

    try {
      if (this.dendrite === null) {
        throw new Error(
          `Axon '${this.neuronId}': not attached to a Dendrite; effector ` +
            "dispatch requires a hosting Dendrite",
        );
      }
      const outcome: ToolOutcome = await this.dendrite.effectorClient.call({
        binding,
        tool: call.tool,
        args: call.args,
        traceId,
        parentId,
        neuron: this.neuronId,
        deadlineMs: binding.defaultDeadlineMs ?? DEFAULT_TOOL_DEADLINE_MS,
        ...(call.callId !== null ? { callId: call.callId } : {}),
      });
      out["effector_id"] = outcome.effectorId;
      if (outcome.error !== null) out["error"] = outcome.error;
      else out["result"] = outcome.result;
    } catch (err) {
      // Tools never kill TASKs - every failure rides the output payload.
      out["error"] =
        err instanceof EffectorError
          ? `${err.name}: ${err.message}`
          : `tool_dispatch_failed: ${err instanceof Error ? err.message : String(err)}`;
    }
    return wrap();
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

/**
 * System-prompt fragment teaching an LLM the `cosmo` intent convention.
 * Without it a hosted model never knows it *can* clarify / request permission
 * / signal a structured error, so the recognisers have nothing to recognise.
 * `Axon.fromSource(..., { recognize: true })` (the default) appends this to
 * the source's `system` prompt for system-capable LLM sources; opt out with
 * `teachIntents: false`.
 */
export const COSMO_INTENT_SYSTEM_PROMPT =
  'You can control the surrounding agent protocol by replying with a single ' +
  'JSON object carrying a "cosmo" key (either as your whole reply or inside ' +
  "a ```json fenced block):\n" +
  '{"cosmo": "clarification", "question": "<what you need to know>"} ' +
  "- ask the orchestrator a question when the task is ambiguous.\n" +
  '{"cosmo": "permission", "action": "<action>", "scope": {...}, "reason": "<why>"} ' +
  "- request approval before a sensitive action.\n" +
  '{"cosmo": "error", "code": "<CODE>", "message": "<details>"} ' +
  "- report a structured failure.\n" +
  '{"cosmo": "output", "output": {...}} - return a structured result.\n' +
  "For a normal answer, just reply with plain text - do not wrap ordinary " +
  "answers in a cosmo object.";

/** Sources whose Neuron wrapper accepts a `system` option. */
const SYSTEM_CAPABLE_SOURCES: ReadonlySet<string> = new Set([
  "ollama",
  "openai",
  "anthropic",
  "groq",
  "openrouter",
  "together",
  "mistral",
]);

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
