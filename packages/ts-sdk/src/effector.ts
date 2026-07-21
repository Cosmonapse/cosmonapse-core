/**
 * @cosmonapse/sdk  -  Effector (action layer)
 *
 * Ported from `cosmonapse.effector.base`. Effector is the action wrapper for
 * Cosmonapse - the synapse-side participant that services TOOL_CALL signals,
 * the way an Engram services RECALL / IMPRINT. In nervous-system terms:
 * Neurons think, Engrams remember, Effectors act.
 *
 * Effectors are addressed by `effectorId` (explicit) or `effectorKind`
 * (typed). One Effector per tool family is the intended deployment:
 * filesystem, shell, websearch, fetch. Tool calls are part of the TASK trace -
 * they inherit the containing TASK's trace_id; the parent_id chain proves
 * causation.
 *
 * Effectors are *not* Neurons. They do not think and never produce
 * AGENT_OUTPUT; a failed invocation surfaces as `error` on TOOL_RESULT rather
 * than an ERROR signal, so the parent TASK is not terminated. They are mounted
 * on a hosting Dendrite via `dendrite.attachEffector(effector)`.
 *
 * Signal pair: TOOL_CALL (request) / TOOL_RESULT (reply, correlated by
 * `parent_id === the TOOL_CALL's id`) - the same per-operation correlation
 * RECALL/RECALLED uses; the caller-side client lives in `effector-client.ts`,
 * built exactly like EngramClient.
 *
 * Host-side behaviour (the standard wiring pattern - mirrors `axon.host`):
 * `effector.host.on*(fn, filter?)` queues a Dendrite signal-handler
 * registration at module scope; the hosting Dendrite replays it right after it
 * connects this Effector (during `start()` / `attachEffector` on a running
 * Dendrite) and ensures the inbound subscription. TOOL_CALL/TOOL_RESULT
 * servicing itself stays `effector.onToolCall` - the host proxy is for
 * observing the *rest* of the protocol (e.g. `host.onFinal` for trace-scoped
 * cleanup).
 */

import { SignalType, type Directed, type Json } from "./envelope.js";
import {
  LifecycleHooks,
  type ConnectHook,
  type RefreshHook,
  type ScheduleHook,
} from "./hooks.js";
// Type-only import: erased at runtime under verbatimModuleSyntax, so this does
// NOT introduce a runtime import cycle with dendrite.ts.
import type { Dendrite, HandlerFilter, SignalHandler } from "./dendrite.js";

/**
 * Package-internal key for the host-registration replay handshake - the
 * Effector-side counterpart of the Axon's APPLY_HOST symbol. Not re-exported
 * from index.ts, so only same-package code (the Dendrite) can invoke it.
 */
export const EFFECTOR_APPLY_HOST: unique symbol = Symbol("cosmonapse.effector.applyHost");

// ---------------------------------------------------------------------------
// Public data types
// ---------------------------------------------------------------------------

export interface ToolOutcomeInit {
  tool: string;
  result?: unknown;
  error?: string | null;
  callId?: string | null;
  tookMs?: number | null;
  effectorId?: string | null;
}

/**
 * What an invoke() call returns to the caller. Exactly one of `result` /
 * `error` should be set. `error` is a tool-level failure the calling Neuron is
 * expected to react to (a missing file, a refused command) - it rides
 * TOOL_RESULT and never terminates the parent TASK.
 *
 * A class (not an interface) so an `onToolCall` handler can return a
 * ready-made outcome and the served Effector can recognise it by instanceof -
 * the TS counterpart of Python's `isinstance(result, ToolOutcome)`.
 */
export class ToolOutcome {
  readonly tool: string;
  readonly result: unknown;
  readonly error: string | null;
  readonly callId: string | null;
  readonly tookMs: number | null;
  readonly effectorId: string | null;

  constructor(init: ToolOutcomeInit) {
    this.tool = init.tool;
    this.result = init.result ?? null;
    this.error = init.error ?? null;
    this.callId = init.callId ?? null;
    this.tookMs = init.tookMs ?? null;
    this.effectorId = init.effectorId ?? null;
  }

  /** Convenience: true when `error` is null. */
  get ok(): boolean {
    return this.error === null;
  }
}

export interface EffectorBindingInit {
  name: string;
  directedId?: string;
  directedType?: string;
  defaultDeadlineMs?: number;
  /** Caller-side routing table: the tool names this binding serves. */
  tools?: string[];
}

/**
 * Declarative wiring of one Effector into an Axon.
 *
 * The Axon stores a list of these at construction time so the Neuron can
 * address Effectors by a stable local name (e.g. `"fs"`) rather than the
 * deployment-specific effectorId. `name` is what the Neuron sees;
 * `directedId` and `directedType` determine how TOOL_CALL is routed on the
 * wire (they become `directed.id` / `directed.type` on the envelope).
 *
 * At least one of `directedId` or `directedType` must be set. `directedId`
 * (the effectorId) is preferred for predictable routing; `directedType` (the
 * effectorKind) is for slot-based routing where deployment owns the concrete
 * impl.
 *
 * `tools` is the caller-side routing table: the tool names this binding
 * serves. The Axon resolves a native tool call to a binding by (1) a binding
 * whose `tools` lists the name, (2) a binding *named* after the tool, (3) the
 * only binding, when there is exactly one. Leave it unset on a single-binding
 * Axon.
 */
export class EffectorBinding {
  readonly name: string;
  readonly directedId: string | null;
  readonly directedType: string | null;
  readonly defaultDeadlineMs: number | null;
  readonly tools: readonly string[] | null;

  constructor(init: EffectorBindingInit) {
    this.name = init.name;
    this.directedId = init.directedId ?? null;
    this.directedType = init.directedType ?? null;
    this.defaultDeadlineMs = init.defaultDeadlineMs ?? null;
    this.tools = init.tools ? [...init.tools] : null;
    if (!this.directedId && !this.directedType) {
      throw new Error(
        `EffectorBinding '${this.name}' requires directedId (effector_id) or ` +
          "directedType (effector_kind), or both",
      );
    }
  }

  /** Build the `Directed` addressing this Effector. */
  toDirected(): Directed {
    return { id: this.directedId, type: this.directedType, capabilities: [] };
  }
}

// ---------------------------------------------------------------------------
// Exceptions
// ---------------------------------------------------------------------------

/** Base for Effector-related errors. */
export class EffectorError extends Error {
  constructor(message?: string) {
    super(message);
    this.name = new.target.name;
  }
}
/** Raised when a TOOL_CALL deadline elapses with no TOOL_RESULT. */
export class EffectorTimeout extends EffectorError {}
/** Raised when the containing TASK terminates while a tool call is in flight
 *  (FINAL/ERROR on the trace, or Dendrite shutdown). */
export class EffectorCancelled extends EffectorError {}
/** Raised when a Neuron asks for an Effector binding name the Axon was not
 *  constructed with. */
export class EffectorNotBound extends EffectorError {}
/** Raised by an Effector backend when it must shed load. Surfaces as `error`
 *  on TOOL_RESULT rather than a separate ERROR signal so the parent TASK is
 *  not terminated. */
export class EffectorOverloaded extends EffectorError {}

// ---------------------------------------------------------------------------
// Host proxy
// ---------------------------------------------------------------------------

/** One queued `effector.host` registration, replayed onto the hosting Dendrite. */
interface HostRegistration {
  type: SignalType;
  fn: SignalHandler;
  filter?: HandlerFilter;
}

/**
 * Deferred Dendrite signal decorators, declared on the Effector - mirrors
 * {@link AxonHost} exactly, for the action side:
 *
 * ```ts
 * const FX = Effector.serve({ effectorId: "fs-effector", effectorKind: "filesystem" });
 * FX.onToolCall(async (tool, args) => { ... });
 * FX.host.onFinal(async (sig) => { ... });   // live once the host starts
 * await dendrite.attachEffector(FX);
 * await dendrite.start();
 * ```
 */
export class EffectorHost {
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

  // -- cognition --
  onToolCallSignal(fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    return this.onSignal(SignalType.TOOL_CALL, fn, filter);
  }
  onToolResult(fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    return this.onSignal(SignalType.TOOL_RESULT, fn, filter);
  }
  onPlan(fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    return this.onSignal(SignalType.PLAN, fn, filter);
  }
  onThoughtDelta(fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    return this.onSignal(SignalType.THOUGHT_DELTA, fn, filter);
  }
  onMemoryAppend(fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    return this.onSignal(SignalType.MEMORY_APPEND, fn, filter);
  }
  onEscalation(fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    return this.onSignal(SignalType.ESCALATION, fn, filter);
  }
}

// ---------------------------------------------------------------------------
// Effector contract
// ---------------------------------------------------------------------------

export interface InvokeOptions {
  callId?: string | null;
  deadlineMs?: number | null;
  traceId?: string | null;
}

/**
 * Action wrapper. One tool family per Effector instance.
 *
 * Every backend implements this exact interface. Subclasses set `effectorId`,
 * `effectorKind` and `capabilities` (the tool names served, e.g.
 * `["read", "write", "glob"]`) on construction. Lifecycle methods (`connect` /
 * `close`) own backend resources (subprocesses, HTTP pools, spawned MCP
 * servers).
 *
 * Effectors do not think. `invoke` performs exactly the named tool call and
 * reports what happened; deciding *which* tool to call, and reacting to the
 * outcome, is Neuron-side work.
 */
export abstract class Effector {
  abstract effectorId: string;
  abstract effectorKind: string;
  abstract capabilities: string[];
  version: string | null = null;

  /** Deferred host-side registrations (`effector.host.on*`), replayed onto
   *  the hosting Dendrite when it connects this Effector. */
  private readonly hostRegs: HostRegistration[] = [];
  private hostRegsApplied = false;
  /** Deferred Dendrite decorators - see {@link EffectorHost}. */
  readonly host: EffectorHost = new EffectorHost(this.hostRegs);

  /** @internal The hosting Dendrite, set by `Dendrite.attachEffector` /
   *  cleared by `detachEffector` - the action-side analogue of an Axon's
   *  dendrite back-reference. */
  _dendrite: Dendrite | null = null;

  /** The hosting Dendrite, once attached. */
  get dendrite(): Dendrite | null {
    return this._dendrite;
  }

  /**
   * Package-internal: invoked by the hosting Dendrite (via
   * {@link EFFECTOR_APPLY_HOST}) right after it connects this Effector.
   * Replays every `effector.host` registration onto the Dendrite and ensures
   * the inbound subscriptions. Applied exactly once per Effector instance.
   */
  async [EFFECTOR_APPLY_HOST](dendrite: Dendrite): Promise<void> {
    if (this.hostRegsApplied || this.hostRegs.length === 0) return;
    this.hostRegsApplied = true;
    const types = new Set<SignalType>();
    for (const { type, fn, filter } of this.hostRegs) {
      dendrite.onSignal(type, fn, filter);
      types.add(type);
    }
    await dendrite.ensureSubscribed(...types);
  }

  /** Open backend resources (subprocess, HTTP pool, ...). */
  abstract connect(): Promise<void>;
  /** Release backend resources. */
  abstract close(): Promise<void>;

  /** Whether this Effector serves `tool`. Default: the tool name is in
   *  `capabilities` (an empty list means serve everything). Backends may
   *  override for dynamic tool sets. */
  async canServe(tool: string): Promise<boolean> {
    return this.capabilities.length === 0 || this.capabilities.includes(tool);
  }

  /**
   * Run one tool call and return the outcome.
   *
   * Tool-level failures (bad args, missing file, non-zero exit) must be
   * reported as `new ToolOutcome({ error })`, not thrown - a Neuron is
   * expected to read the error and react. Throw only for backend faults
   * (broken subprocess, lost connection); the hosting Dendrite maps a thrown
   * error onto TOOL_RESULT `error` anyway, so the parent TASK is never
   * terminated by a tool.
   */
  abstract invoke(tool: string, args: Json, opts?: InvokeOptions): Promise<ToolOutcome>;

  /**
   * Build an Effector from the one protocol hook that matters.
   *
   * Cosmonapse does not build your tools - no registries, no frameworks. It
   * gives you exactly the signal pair: a TOOL_CALL arrives, your handler
   * runs, its return value is emitted as the TOOL_RESULT. What happens in
   * between (dispatch tables, MCP sessions, subprocesses, sandboxing) is your
   * code:
   *
   * ```ts
   * const FX = Effector.serve({ effectorId: "fs-effector", effectorKind: "filesystem" });
   * FX.onToolCall(async (tool, args) => {
   *   if (tool === "read") return { content: await readFile(args.path) };
   *   return null;          // fall through / unknown
   * });
   * await dendrite.attachEffector(FX);
   * ```
   *
   * The result is a plain Effector. Lifecycle follows the shared
   * {@link LifecycleHooks} contract every other component uses: `onConnect`
   * fires once when the hosting Dendrite connects the Effector at start(),
   * `onSchedule(everyMs)` loops run until stop(), `onRefresh` fires on
   * `await fx.refresh()`. Hooks receive the owner (the Effector) as first
   * argument.
   */
  static serve(opts: {
    effectorId: string;
    effectorKind?: string;
    version?: string | null;
  }): ServedEffector {
    return new ServedEffector({
      effectorId: opts.effectorId,
      effectorKind: opts.effectorKind ?? "effector",
      version: opts.version ?? null,
    });
  }
}

// ---------------------------------------------------------------------------
// Protocol-hook Effectors
// ---------------------------------------------------------------------------

/** Context handed to an `onToolCall` handler as its third argument - the TS
 *  counterpart of the kwargs a Python handler opts into by declaring them. */
export interface ToolCallContext {
  callId: string | null;
  deadlineMs: number | null;
  traceId: string | null;
}

/**
 * A TOOL_CALL handler; its RETURN VALUE IS EMITTED AS THE TOOL_RESULT - no
 * manual publish. Multiple handlers run in registration order; the first
 * non-null return answers, null/undefined falls through (so a policy gate can
 * sit in front of a proxy). A throw becomes `error` on the TOOL_RESULT - a
 * tool never terminates the parent TASK. If every handler returns null the
 * reply is an `unhandled tool` error.
 */
export type ToolCallHandler = (
  tool: string,
  args: Json,
  ctx: ToolCallContext,
) => unknown | Promise<unknown>;

/**
 * Concrete Effector with one tool surface - `onToolCall`, whose return value
 * is emitted as the TOOL_RESULT by the hosting Dendrite - plus the shared
 * LifecycleHooks trio. Built by {@link Effector.serve}; not instantiated
 * directly.
 */
export class ServedEffector extends Effector {
  effectorId: string;
  effectorKind: string;
  capabilities: string[];

  /** @internal - lifecycle hooks, driven by the hosting Dendrite. */
  readonly hooks: LifecycleHooks<ServedEffector> = new LifecycleHooks<ServedEffector>(this);

  /** TOOL_CALL handlers, tried in registration order. */
  private readonly callHandlers: ToolCallHandler[] = [];

  constructor(opts: { effectorId: string; effectorKind: string; version: string | null }) {
    super();
    this.effectorId = opts.effectorId;
    this.effectorKind = opts.effectorKind;
    this.capabilities = [];
    this.version = opts.version;
  }

  /** Register a TOOL_CALL handler - see {@link ToolCallHandler}. */
  onToolCall(fn: ToolCallHandler): ToolCallHandler {
    this.callHandlers.push(fn);
    return fn;
  }

  /** Register a fire-once handler called when the hosting Dendrite connects
   *  this Effector at start(). */
  onConnect(fn: ConnectHook<ServedEffector>): ConnectHook<ServedEffector> {
    return this.hooks.onConnect(fn);
  }
  /** Register a handler fired on `await fx.refresh()`. */
  onRefresh(fn: RefreshHook<ServedEffector>): RefreshHook<ServedEffector> {
    return this.hooks.onRefresh(fn);
  }
  /** Register a periodic handler that runs every `everyMs` while the host runs. */
  onSchedule(everyMs: number, fn: ScheduleHook<ServedEffector>): ScheduleHook<ServedEffector> {
    return this.hooks.onSchedule(everyMs, fn);
  }
  /** Fire the onRefresh hooks. */
  async refresh(
    opts: { reason?: string; extra?: Record<string, unknown> } = {},
  ): Promise<void> {
    await this.hooks.refresh(opts);
  }

  // -- Effector interface ----------------------------------------------

  /** A served Effector answers for every tool name once a handler is
   *  registered - routing between tools is the handler's job. */
  override async canServe(_tool: string): Promise<boolean> {
    return this.callHandlers.length > 0;
  }

  /** Called by the hosting Dendrite at start(): starts the `onSchedule`
   *  loops and fires the `onConnect` hooks. */
  async connect(): Promise<void> {
    this.hooks._launchSchedule();
    await this.hooks._fireConnect();
  }

  /** Called by the hosting Dendrite at stop()/detach: cancels the
   *  `onSchedule` loops. */
  async close(): Promise<void> {
    this.hooks._stopHooks();
  }

  async invoke(tool: string, args: Json, opts: InvokeOptions = {}): Promise<ToolOutcome> {
    const t0 = Date.now();
    const ctx: ToolCallContext = {
      callId: opts.callId ?? null,
      deadlineMs: opts.deadlineMs ?? null,
      traceId: opts.traceId ?? null,
    };
    for (const fn of this.callHandlers) {
      let result: unknown;
      try {
        result = await fn(tool, args, ctx);
      } catch (err) {
        // Tools never kill TASKs.
        return new ToolOutcome({
          tool,
          callId: ctx.callId,
          error: err instanceof Error ? `${err.name}: ${err.message}` : String(err),
          tookMs: Date.now() - t0,
          effectorId: this.effectorId,
        });
      }
      if (result === null || result === undefined) continue;
      if (result instanceof ToolOutcome) return result;
      return new ToolOutcome({
        tool,
        result,
        callId: ctx.callId,
        tookMs: Date.now() - t0,
        effectorId: this.effectorId,
      });
    }
    return new ToolOutcome({
      tool,
      callId: ctx.callId,
      error: `unhandled tool '${tool}': no onToolCall handler answered`,
    });
  }
}
