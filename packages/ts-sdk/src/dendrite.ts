/**
 * @cosmonapse/sdk  -  dendrite
 *
 * The synapse-side participant, ported from `cosmonapse.dendrite`.
 *
 * Construction is minimal: only `synapse` is required. Everything else is
 * opt-in:
 *   - Attach Axons      -> subscribes to TASK, emits REGISTER / HEARTBEAT /
 *                          DEREGISTER, routes inbound TASKs to the right Axon.
 *   - Register handlers -> subscribes to that AXON_TYPE and dispatches.
 *   - heartbeatMs = 0   -> the heartbeat loop never starts.
 *
 * The Dendrite does NOT own the Synapse  -  the caller builds and closes it.
 *
 * There is no separate Cortex class: every Dendrite has dispatchTask /
 * emitFinal / emitError / emit plus the inbound-handler hooks. `Cortex` is
 * kept as a back-compat alias.
 *
 * Lifecycle: call `await dendrite.start()` / `await dendrite.stop()`, or use
 * `await using dendrite = new Dendrite({...}); await dendrite.start();`  -  the
 * Symbol.asyncDispose implementation calls stop() automatically when the scope
 * exits. This is the TS counterpart to Python's `async with dendrite:`.
 *
 * LifecycleHooks (onConnect / onRefresh / onSchedule) are wired in: connect
 * hooks fire and schedule loops launch at the end of start(); refresh hooks
 * fire on every heartbeat tick and whenever a REGISTER / DEREGISTER / HEARTBEAT
 * updates the registry; all loops stop in stop(). Attached Axons' hooks are
 * driven alongside the Dendrite's own.
 */

import { Axon, ATTACH } from "./axon.js";
import {
  LifecycleHooks,
  type ConnectHook,
  type RefreshHook,
  type ScheduleHook,
} from "./hooks.js";
import {
  AXON_TYPES,
  SignalType,
  SYNAPSE_TYPES,
  type Json,
  type Signal,
} from "./envelope.js";
import {
  clarificationAnswerSignal,
  deregisterSignal,
  errorSignal,
  finalSignal,
  heartbeatSignal,
  permissionDecisionSignal,
  registerSignal,
  taskSignal,
} from "./signals.js";
import type { MessageHandler, Subscription, Synapse } from "./synapse.js";
import {
  neuronRecord,
  type ListOptions,
  type NeuronRecord,
  type NeuronStatus,
  type RegistryStore,
} from "./storage.js";

// --- explicit resource management (`await using`) ------------------------
//
// `Symbol.asyncDispose` lands in the standard lib only from the esnext.disposable
// definitions (TS 5.2+) and exists at runtime on Node 20+. This package targets
// `lib: ["ES2022"]` and `node >= 18`, so we (a) augment the type and (b) install
// a runtime shim if the symbol is missing. The `??=` makes both idempotent and
// non-destructive on runtimes that already provide it.
declare global {
  interface SymbolConstructor {
    readonly asyncDispose: unique symbol;
  }
}
(Symbol as { asyncDispose?: symbol }).asyncDispose ??= Symbol.for("Symbol.asyncDispose");

export type SignalHandler = (signal: Signal) => void | Promise<void>;

/** Raised when an emit violates the protocol (e.g. emitting an Axon-only type). */
export class DendriteProtocolError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "DendriteProtocolError";
  }
}
export { DendriteProtocolError as CortexProtocolError };

export interface DendriteOptions {
  synapse: Synapse;
  /** Optional registry. When set, the Dendrite mirrors its own Axons and
   *  tracks the namespace-wide Neuron view from REGISTER/DEREGISTER/HEARTBEAT. */
  registryStore?: RegistryStore;
  namespace?: string;
  dendriteId?: string;
  /** Per-attached-Axon heartbeat interval in ms. 0 disables the loop. */
  heartbeatMs?: number;
  /** Re-emit REGISTER on every heartbeat tick so late joiners catch up. */
  reregisterOnHeartbeat?: boolean;
}

export class Dendrite {
  readonly synapse: Synapse;
  readonly registryStore: RegistryStore | null;
  readonly namespace: string;
  readonly dendriteId: string;
  private readonly heartbeatMs: number;
  private readonly reregisterOnHeartbeat: boolean;

  private readonly _axons = new Map<string, Axon>();
  private readonly handlers = new Map<SignalType, SignalHandler[]>();
  private taskSub: Subscription | null = null;
  private readonly inboundSubs = new Map<SignalType, Subscription>();
  // Self-scheduling setTimeout handle (not setInterval  -  see startHeartbeatLoop).
  private heartbeatTimer: ReturnType<typeof setTimeout> | null = null;
  // Set true by stop() so an in-flight tick won't re-arm the loop.
  private heartbeatStopped = true;
  private running = false;

  /** @internal  -  lifecycle hooks for this Dendrite. */
  readonly hooks: LifecycleHooks<Dendrite> = new LifecycleHooks<Dendrite>(this);

  constructor(opts: DendriteOptions) {
    if (!opts.synapse) throw new TypeError("Dendrite requires a synapse");
    this.synapse = opts.synapse;
    this.registryStore = opts.registryStore ?? null;
    this.namespace = opts.namespace ?? "default";
    this.dendriteId = opts.dendriteId ?? "dendrite";
    this.heartbeatMs = opts.heartbeatMs ?? 30_000;
    this.reregisterOnHeartbeat = opts.reregisterOnHeartbeat ?? true;
    for (const t of AXON_TYPES) this.handlers.set(t, []);
  }

  // -- properties ----------------------------------------------------

  get axons(): ReadonlyMap<string, Axon> {
    return new Map(this._axons);
  }

  axon(neuronId: string): Axon | undefined {
    return this._axons.get(neuronId);
  }

  // -- attachment ----------------------------------------------------

  attachAxon(axon: Axon): void {
    if (this._axons.has(axon.neuronId)) {
      throw new Error(`Dendrite already has an Axon for neuronId='${axon.neuronId}'`);
    }
    this._axons.set(axon.neuronId, axon);
    axon[ATTACH](this);
  }

  // -- inbound handler registration ----------------------------------

  private on(type: SignalType, fn: SignalHandler): SignalHandler {
    const list = this.handlers.get(type);
    if (!list) {
      throw new DendriteProtocolError(`Cannot handle non-Axon type '${type}'`);
    }
    list.push(fn);
    if (this.running && !this.inboundSubs.has(type)) {
      void this.ensureInboundSub(type);
    }
    return fn;
  }

  onAgentOutput(fn: SignalHandler): SignalHandler {
    return this.on(SignalType.AGENT_OUTPUT, fn);
  }
  onClarification(fn: SignalHandler): SignalHandler {
    return this.on(SignalType.CLARIFICATION, fn);
  }
  /**
   * Register a handler fired on inbound PERMISSION requests - the *answering*
   * side. A central Cortex or a peer Dendrite evaluates the request (often
   * consulting an Engram of standing grants, keyed per-neuron) and replies via
   * {@link respondToPermission} (re-dispatch a TASK with the verdict) or
   * {@link grantPermission} / {@link denyPermission} (emit a discrete
   * PERMISSION_DECISION). It may also imprint the decision into an Engram so
   * future recalls hit.
   */
  onPermission(fn: SignalHandler): SignalHandler {
    return this.on(SignalType.PERMISSION, fn);
  }
  onErrorSignal(fn: SignalHandler): SignalHandler {
    return this.on(SignalType.ERROR, fn);
  }
  onRegister(fn: SignalHandler): SignalHandler {
    return this.on(SignalType.REGISTER, fn);
  }
  onDeregister(fn: SignalHandler): SignalHandler {
    return this.on(SignalType.DEREGISTER, fn);
  }
  onHeartbeat(fn: SignalHandler): SignalHandler {
    return this.on(SignalType.HEARTBEAT, fn);
  }

  // -- lifecycle hooks ----------------------------------------------

  /** Register a fire-once handler called after start() completes. */
  onConnect(fn: ConnectHook<Dendrite>): ConnectHook<Dendrite> {
    return this.hooks.onConnect(fn);
  }
  /** Register a handler called whenever this Dendrite's state refreshes. */
  onRefresh(fn: RefreshHook<Dendrite>): RefreshHook<Dendrite> {
    return this.hooks.onRefresh(fn);
  }
  /** Register a periodic handler that runs every `everyMs` until stop(). */
  onSchedule(everyMs: number, fn: ScheduleHook<Dendrite>): ScheduleHook<Dendrite> {
    return this.hooks.onSchedule(everyMs, fn);
  }
  /** Manually fire a refresh event (reason defaults to "manual"). */
  async refresh(
    opts: { reason?: string; neuronId?: string | null; extra?: Record<string, unknown> } = {},
  ): Promise<void> {
    await this.hooks.refresh(opts);
  }

  // -- lifecycle -----------------------------------------------------

  async start(): Promise<void> {
    if (this.running) return;

    if (this.registryStore !== null) await this.registryStore.connect();

    // Only subscribe to TASK if there's an Axon to route to.
    if (this._axons.size > 0) {
      this.taskSub = await this.synapse.subscribe(
        this.subject(SignalType.TASK),
        (s) => this.onTask(s),
      );
      for (const axon of this._axons.values()) {
        await this.mirrorToStore(axon, "registered");
        await this.emitRegister(axon);
      }
    }

    for (const [type, hs] of this.handlers) {
      if (hs.length) await this.ensureInboundSub(type);
    }

    // With a store, auto-wire the three management types so it tracks the
    // namespace-wide view (REGISTER from peers, etc.) even without handlers.
    if (this.registryStore !== null) {
      for (const t of [SignalType.REGISTER, SignalType.DEREGISTER, SignalType.HEARTBEAT]) {
        await this.ensureInboundSub(t);
      }
    }

    this.running = true;

    if (this._axons.size > 0 && this.heartbeatMs > 0) {
      this.startHeartbeatLoop();
    }

    // Lifecycle hooks: fire connect handlers and launch schedule loops, for the
    // Dendrite and every attached Axon, now that everything is wired.
    await this.hooks._fireConnect();
    this.hooks._launchSchedule();
    for (const axon of this._axons.values()) {
      await axon.hooks._fireConnect();
      axon.hooks._launchSchedule();
    }
  }

  /**
   * Heartbeat as a self-scheduling async loop rather than `setInterval`.
   *
   * Why not setInterval: it fires on a fixed wall-clock cadence regardless of
   * whether the previous tick finished, so under load ticks overlap and the
   * effective interval drifts; and because the callback is sync, any rejection
   * from the async work inside is an unhandled rejection that setInterval
   * silently drops. Here each tick is fully awaited, its errors are caught, and
   * only then is the next tick scheduled  -  matching the Python SDK's
   * asyncio.Task semantics (structured error handling + clean cancellation).
   */
  private startHeartbeatLoop(): void {
    this.heartbeatStopped = false;

    const schedule = (): void => {
      this.heartbeatTimer = setTimeout(() => {
        void tick();
      }, this.heartbeatMs);
      // Don't keep the event loop alive solely for heartbeats.
      (this.heartbeatTimer as { unref?: () => void }).unref?.();
    };

    const tick = async (): Promise<void> => {
      if (this.heartbeatStopped || !this.running) return;
      try {
        await this.heartbeatTick();
      } catch {
        // Structured handling: a throw must never kill the loop or surface as
        // an unhandled rejection. heartbeatTick is already best-effort per Axon;
        // this is the backstop.
      }
      if (!this.heartbeatStopped && this.running) schedule();
    };

    schedule();
  }

  async stop(reason?: string): Promise<void> {
    if (!this.running) return;
    this.running = false;

    // Stop all lifecycle-hook schedule loops (Dendrite + Axons).
    this.hooks._stopHooks();
    for (const axon of this._axons.values()) axon.hooks._stopHooks();

    // Cancel the heartbeat loop: flag first so an in-flight tick won't re-arm,
    // then clear any pending timer.
    this.heartbeatStopped = true;
    if (this.heartbeatTimer !== null) {
      clearTimeout(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }

    if (this.taskSub !== null) {
      await this.taskSub.unsubscribe();
      this.taskSub = null;
    }

    for (const sub of this.inboundSubs.values()) {
      try {
        await sub.unsubscribe();
      } catch {
        /* best-effort teardown */
      }
    }
    this.inboundSubs.clear();

    for (const axon of this._axons.values()) {
      if (this.registryStore !== null) {
        try {
          await this.registryStore.markDeregistered(axon.neuronId);
        } catch {
          /* best-effort */
        }
      }
      await this.emitDeregister(axon, reason);
    }
    // NOTE: the Dendrite does NOT own the Synapse (nor the registry store).
    // The caller closes them.
  }

  /**
   * Explicit-resource-management hook so a Dendrite can be used with
   * `await using`  -  the TS equivalent of Python's `async with dendrite:`.
   *
   * ```ts
   * await using dendrite = new Dendrite({ synapse });
   * dendrite.attachAxon(axon);
   * await dendrite.start();
   * // ... stop() runs automatically when this scope exits, even on throw.
   * ```
   *
   * Idempotent: stop() is a no-op if the Dendrite was never started or already
   * stopped. As with stop(), the caller still owns the Synapse/registry store.
   */
  async [Symbol.asyncDispose](): Promise<void> {
    await this.stop();
  }

  // -- registry helpers ----------------------------------------------

  private requireStore(): RegistryStore {
    if (this.registryStore === null) {
      throw new Error(
        "Dendrite has no registryStore  -  pass one at construction to use " +
          "registry helpers (findNeurons / registrySnapshot).",
      );
    }
    return this.registryStore;
  }

  /** All known records, optionally filtered (live records only by default). */
  async registrySnapshot(opts: ListOptions = {}): Promise<NeuronRecord[]> {
    return this.requireStore().list(opts);
  }

  /** Live (non-deregistered) records, optionally filtered by capability. */
  async findNeurons(opts: { capability?: string } = {}): Promise<NeuronRecord[]> {
    return this.requireStore().list({
      ...(opts.capability !== undefined ? { capability: opts.capability } : {}),
      includeDeregistered: false,
    });
  }

  // -- outbound primitives ------------------------------------------

  async dispatchTask(args: {
    neuron: string;
    input: Json;
    traceId?: string;
    parentId?: string | null;
    contextRef?: string;
    capabilities?: string[];
    meta?: Json;
  }): Promise<Signal> {
    const sig = taskSignal({
      directed: { id: args.neuron },
      input: args.input,
      ...(args.traceId !== undefined ? { traceId: args.traceId } : {}),
      ...(args.parentId !== undefined ? { parentId: args.parentId } : {}),
      ...(args.contextRef !== undefined ? { contextRef: args.contextRef } : {}),
      ...(args.capabilities !== undefined ? { capabilities: args.capabilities } : {}),
      ...(args.meta !== undefined ? { meta: args.meta } : {}),
    });
    await this.emit(sig);
    return sig;
  }

  async emitFinal(args: {
    traceId: string;
    parentId: string;
    result: Json;
    meta?: Json;
  }): Promise<Signal> {
    const sig = finalSignal({
      traceId: args.traceId,
      parentId: args.parentId,
      result: args.result,
      directed: { id: this.dendriteId },
      ...(args.meta !== undefined ? { meta: args.meta } : {}),
    });
    await this.emit(sig);
    return sig;
  }

  async emitError(args: {
    traceId: string;
    parentId?: string | null;
    code: string;
    message: string;
    recoverable?: boolean;
    meta?: Json;
  }): Promise<Signal> {
    const sig = errorSignal({
      traceId: args.traceId,
      code: args.code,
      message: args.message,
      directed: { id: this.dendriteId },
      ...(args.parentId !== undefined ? { parentId: args.parentId } : {}),
      ...(args.recoverable !== undefined ? { recoverable: args.recoverable } : {}),
      ...(args.meta !== undefined ? { meta: args.meta } : {}),
    });
    await this.emit(sig);
    return sig;
  }

  /** Emit a synapse-side Signal. Refuses Axon-owned types. */
  /**
   * Reply to a PERMISSION by re-dispatching a TASK carrying the verdict.
   *
   * The "send it back to the axon" path: the follow-up TASK is addressed by
   * default to the Neuron that asked (`signal.neuron`), with `parentId` = the
   * PERMISSION's id and the original `traceId` carried over, so the Neuron
   * resumes on the same thread and can imprint the decision into an Engram (or
   * recall it next time). New TASK input: `{ permission: { action, granted,
   * reason?, ttlMs?, ...extra } }`.
   */
  async respondToPermission(
    request: Signal,
    opts: {
      granted: boolean;
      reason?: string;
      ttlMs?: number;
      extra?: Json;
      neuron?: string;
      meta?: Json;
    },
  ): Promise<Signal> {
    if (request.type !== SignalType.PERMISSION) {
      throw new DendriteProtocolError(
        `respondToPermission expects a PERMISSION signal, got '${request.type}'`,
      );
    }
    const target = opts.neuron ?? request.directed?.id ?? null;
    if (!target) {
      throw new DendriteProtocolError(
        "respondToPermission: signal has no neuron and no neuron override - " +
          "nowhere to dispatch the follow-up TASK",
      );
    }
    const permission: Json = {
      action: request.payload["action"] ?? null,
      granted: opts.granted,
    };
    if (opts.reason !== undefined) permission["reason"] = opts.reason;
    if (opts.ttlMs !== undefined) permission["ttl_ms"] = opts.ttlMs;
    if (opts.extra !== undefined) Object.assign(permission, opts.extra);
    return this.dispatchTask({
      neuron: target,
      input: { permission },
      traceId: request.trace_id,
      parentId: request.id,
      ...(opts.meta !== undefined ? { meta: opts.meta } : {}),
    });
  }

  // -- cognition decision signals (discrete, decentralised option) -----
  // Thin, stateless emit helpers for the new response signal types - no
  // correlation client. Use these when you want the decision to travel as a
  // discrete PERMISSION_DECISION / CLARIFICATION_ANSWER signal (e.g. for a
  // peer/observer to imprint into an Engram) rather than as a re-dispatched
  // TASK. Published via `publish` so any Dendrite - including a peer - can
  // answer; correlation, if needed, is the developer's choice.

  /** Approve a PERMISSION request. `ttlMs` optionally advertises how long the
   * grant is valid so the requester can cache it (e.g. in an Engram). */
  async grantPermission(
    request: Signal,
    opts: { reason?: string; ttlMs?: number; meta?: Json } = {},
  ): Promise<Signal> {
    return this.decidePermission(request, true, opts);
  }

  /** Reject a PERMISSION request. */
  async denyPermission(
    request: Signal,
    opts: { reason?: string; meta?: Json } = {},
  ): Promise<Signal> {
    return this.decidePermission(request, false, opts);
  }

  private async decidePermission(
    request: Signal,
    granted: boolean,
    opts: { reason?: string; ttlMs?: number; meta?: Json },
  ): Promise<Signal> {
    if (request.type !== SignalType.PERMISSION) {
      throw new DendriteProtocolError(
        `grant/denyPermission expects a PERMISSION signal, got '${request.type}'`,
      );
    }
    const sig = permissionDecisionSignal({
      traceId: request.trace_id,
      parentId: request.id,
      granted,
      directed: { id: this.dendriteId },
      ...(opts.reason !== undefined ? { reason: opts.reason } : {}),
      ...(opts.ttlMs !== undefined ? { ttlMs: opts.ttlMs } : {}),
      ...(opts.meta !== undefined ? { meta: opts.meta } : {}),
    });
    await this.publish(sig);
    return sig;
  }

  /** Answer a *blocking* CLARIFICATION (the Neuron called ask(...) and is
   * awaiting). Distinct from the legacy return-marker flow. */
  async answerClarification(
    request: Signal,
    answer: unknown,
    opts: { meta?: Json } = {},
  ): Promise<Signal> {
    if (request.type !== SignalType.CLARIFICATION) {
      throw new DendriteProtocolError(
        `answerClarification expects a CLARIFICATION signal, got '${request.type}'`,
      );
    }
    const sig = clarificationAnswerSignal({
      traceId: request.trace_id,
      parentId: request.id,
      answer,
      directed: { id: this.dendriteId },
      ...(opts.meta !== undefined ? { meta: opts.meta } : {}),
    });
    await this.publish(sig);
    return sig;
  }

  async emit(signal: Signal): Promise<void> {
    if (!SYNAPSE_TYPES.has(signal.type)) {
      throw new DendriteProtocolError(
        `Dendrite refuses to emit '${signal.type}': only synapse-side types ` +
          `may be emitted this way. '${signal.type}' is an Axon-owned type.`,
      );
    }
    await this.publish(signal);
  }

  async publish(signal: Signal): Promise<void> {
    await this.synapse.publish(this.subject(signal.type), signal);
  }

  async subscribe(
    type: SignalType,
    handler: MessageHandler,
    opts?: { queueGroup?: string },
  ): Promise<Subscription> {
    return this.synapse.subscribe(this.subject(type), handler, opts);
  }

  // -- internal ------------------------------------------------------

  private subject(type: SignalType): string {
    return `cosmonapse.${this.namespace}.${type}`;
  }

  private async ensureInboundSub(type: SignalType): Promise<void> {
    if (this.inboundSubs.has(type)) return;
    const sub = await this.subscribe(type, (s) => this.dispatchInbound(s));
    this.inboundSubs.set(type, sub);
  }

  private async onTask(task: Signal): Promise<void> {
    const target = task.directed?.id ?? null;
    if (!target) return;
    const axon = this._axons.get(target);
    if (!axon) return;

    let reply: Signal;
    try {
      reply = await axon.handleTask(task);
    } catch (err) {
      reply = errorSignal({
        traceId: task.trace_id,
        parentId: task.id,
        directed: { id: target },
        code: "AXON_EXCEPTION",
        message: err instanceof Error ? err.message : String(err),
        recoverable: false,
      });
    }
    await this.publish(reply);
  }

  private async emitRegister(axon: Axon): Promise<void> {
    await this.publish(
      registerSignal({
        directed: { id: axon.neuronId, capabilities: [...axon.capabilities] },
        capabilities: axon.capabilities,
        ...(axon.version !== undefined ? { version: axon.version } : {}),
      }),
    );
  }

  private async emitDeregister(axon: Axon, reason?: string): Promise<void> {
    await this.publish(
      deregisterSignal({
        directed: { id: axon.neuronId },
        ...(reason !== undefined ? { reason } : {}),
      }),
    );
  }

  private async heartbeatTick(): Promise<void> {
    if (!this.running) return;
    const now = new Date().toISOString();
    for (const axon of this._axons.values()) {
      try {
        if (this.reregisterOnHeartbeat) await this.emitRegister(axon);
        await this.synapse.publish(
          this.subject(SignalType.HEARTBEAT),
          heartbeatSignal({ directed: { id: axon.neuronId } }),
        );
      } catch {
        /* best-effort heartbeat */
      }
      if (this.registryStore !== null) {
        try {
          await this.registryStore.touchHeartbeat(axon.neuronId, now);
        } catch {
          /* best-effort */
        }
      }
      await this.hooks._fireRefresh({ reason: "heartbeat", neuronId: axon.neuronId, extra: {} });
      await axon.hooks._fireRefresh({ reason: "heartbeat", neuronId: axon.neuronId, extra: {} });
    }
  }

  private async mirrorToStore(axon: Axon, status: NeuronStatus): Promise<void> {
    if (this.registryStore === null) return;
    try {
      await this.registryStore.upsert(
        neuronRecord({
          neuron_id: axon.neuronId,
          capabilities: [...axon.capabilities],
          version: axon.version ?? null,
          status,
          last_heartbeat: new Date().toISOString(),
        }),
      );
    } catch {
      /* best-effort mirror */
    }
  }

  private async dispatchInbound(signal: Signal): Promise<void> {
    if (!AXON_TYPES.has(signal.type)) return;
    if (this.registryStore !== null) {
      try {
        await this.updateRegistry(signal);
      } catch {
        /* best-effort registry update */
      }
    }
    const handlers = this.handlers.get(signal.type) ?? [];
    if (!handlers.length) return;
    await Promise.allSettled(handlers.map((h) => h(signal)));
  }

  private async updateRegistry(signal: Signal): Promise<void> {
    if (this.registryStore === null) return;
    // Engram registrations (REGISTER with the `engram` flag) are not Neurons;
    // do not mirror them into the Neuron registry store.
    if (signal.payload["engram"]) return;
    const neuronId = signal.directed?.id ?? null;
    if (!neuronId) return;
    let reason: string | null = null;
    if (signal.type === SignalType.REGISTER) {
      await this.registryStore.upsert(
        neuronRecord({
          neuron_id: neuronId,
          capabilities: (signal.payload["capabilities"] as string[] | undefined) ?? [],
          version: (signal.payload["version"] as string | undefined) ?? null,
          status: "registered",
          last_heartbeat: signal.ts,
        }),
      );
      reason = "register";
    } else if (signal.type === SignalType.DEREGISTER) {
      await this.registryStore.markDeregistered(neuronId);
      reason = "deregister";
    } else if (signal.type === SignalType.HEARTBEAT) {
      const status = signal.payload["status"] as NeuronStatus | undefined;
      if (status) await this.registryStore.touchHeartbeat(neuronId, signal.ts, status);
      else await this.registryStore.touchHeartbeat(neuronId, signal.ts);
      reason = "heartbeat";
    }
    if (reason !== null) {
      await this.hooks._fireRefresh({ reason, neuronId, extra: {} });
    }
  }
}

/** Back-compat alias  -  a Cortex is just a Dendrite. */
export const Cortex = Dendrite;
export type Cortex = Dendrite;
