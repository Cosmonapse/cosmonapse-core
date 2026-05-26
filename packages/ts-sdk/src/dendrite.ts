/**
 * @cosmonapse/sdk — dendrite
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
 * The Dendrite does NOT own the Synapse — the caller builds and closes it.
 *
 * There is no separate Cortex class: every Dendrite has dispatchTask /
 * emitFinal / emitError / emit plus the inbound-handler hooks. `Cortex` is
 * kept as a back-compat alias.
 *
 * (Not yet ported from Python: the optional RegistryStore mirror and the
 * LifecycleHooks scheduler. Handlers and routing are fully functional.)
 */

import { Axon } from "./axon.js";
import {
  AXON_TYPES,
  SignalType,
  SYNAPSE_TYPES,
  type Json,
  type Signal,
} from "./envelope.js";
import {
  deregisterSignal,
  errorSignal,
  finalSignal,
  heartbeatSignal,
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
  private heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  private running = false;

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
    axon.attachTo(this);
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
      this.heartbeatTimer = setInterval(() => {
        void this.heartbeatTick();
      }, this.heartbeatMs);
      // Don't keep the event loop alive solely for heartbeats.
      (this.heartbeatTimer as { unref?: () => void }).unref?.();
    }
  }

  async stop(reason?: string): Promise<void> {
    if (!this.running) return;
    this.running = false;

    if (this.heartbeatTimer !== null) {
      clearInterval(this.heartbeatTimer);
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

  // -- registry helpers ----------------------------------------------

  private requireStore(): RegistryStore {
    if (this.registryStore === null) {
      throw new Error(
        "Dendrite has no registryStore — pass one at construction to use " +
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
      neuron: args.neuron,
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
      neuron: this.dendriteId,
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
      neuron: this.dendriteId,
      ...(args.parentId !== undefined ? { parentId: args.parentId } : {}),
      ...(args.recoverable !== undefined ? { recoverable: args.recoverable } : {}),
      ...(args.meta !== undefined ? { meta: args.meta } : {}),
    });
    await this.emit(sig);
    return sig;
  }

  /** Emit a synapse-side Signal. Refuses Axon-owned types. */
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
    const target = task.neuron;
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
        neuron: target,
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
        neuron: axon.neuronId,
        capabilities: axon.capabilities,
        ...(axon.version !== undefined ? { version: axon.version } : {}),
      }),
    );
  }

  private async emitDeregister(axon: Axon, reason?: string): Promise<void> {
    await this.publish(
      deregisterSignal({
        neuron: axon.neuronId,
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
          heartbeatSignal({ neuron: axon.neuronId }),
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
    const neuronId = signal.neuron;
    if (!neuronId) return;
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
    } else if (signal.type === SignalType.DEREGISTER) {
      await this.registryStore.markDeregistered(neuronId);
    } else if (signal.type === SignalType.HEARTBEAT) {
      const status = signal.payload["status"] as NeuronStatus | undefined;
      if (status) await this.registryStore.touchHeartbeat(neuronId, signal.ts, status);
      else await this.registryStore.touchHeartbeat(neuronId, signal.ts);
    }
  }
}

/** Back-compat alias — a Cortex is just a Dendrite. */
export const Cortex = Dendrite;
export type Cortex = Dendrite;
