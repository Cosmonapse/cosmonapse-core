/**
 * @cosmonapse/sdk  -  dendrite
 *
 * The synapse-side participant, ported from `cosmonapse.dendrite`.
 *
 * Construction is minimal: only `synapse` is required. Everything else is
 * opt-in:
 *   - Attach Axons      -> subscribes to TASK (addressed broadcast) and the
 *                          capability-routed subject (queue-grouped), emits
 *                          REGISTER / HEARTBEAT / DEREGISTER, routes inbound
 *                          TASKs to the right Axon.
 *   - Register handlers -> subscribes to that SignalType and dispatches.
 *   - heartbeatMs = 0   -> the heartbeat loop never starts.
 *
 * The Dendrite does NOT own the Synapse  -  the caller builds and closes it.
 *
 * There is no separate Cortex class: every Dendrite has the dispatch family
 * (dispatch / dispatchAndWait / dispatchAndSubscribe / dispatchOffer /
 * dispatchTask), emitFinal / emitError / the cognition emit helpers, plus the
 * inbound-handler hooks. `Cortex` is kept as a back-compat alias.
 *
 * Unified dispatch: `dispatch()` returns a {@link Pathway} scoped to the
 * trace  -  await it, attach callbacks, or iterate. `scope: "terminal"`
 * additionally tags the TASK with `payload.finalize` (terminal-handler
 * finalize): the worker Dendrite that runs the Axon promotes a successful
 * AGENT_OUTPUT by also emitting FINAL, so terminal-scoped Pathways resolve
 * against stock workers.
 *
 * Lifecycle: `await dendrite.start()` / `await dendrite.stop()`, or
 * `await using dendrite = new Dendrite({...})`.
 */

import { Axon, ATTACH, DETACH } from "./axon.js";
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
  newTraceId,
  type Json,
  type Signal,
} from "./envelope.js";
import {
  bidSignal,
  clarificationAnswerSignal,
  consensusSignal,
  contextSyncSignal,
  critiqueSignal,
  deregisterSignal,
  errorSignal,
  stopSignal,
  stoppedSignal,
  escalationSignal,
  finalSignal,
  heartbeatSignal,
  memoryAppendSignal,
  permissionDecisionSignal,
  planSignal,
  registerSignal,
  taskAwardedSignal,
  taskDeclinedSignal,
  taskOfferSignal,
  taskSignal,
  thoughtDeltaSignal,
  toolCallSignal,
  toolResultSignal,
} from "./signals.js";
import { Pathway, PATHWAY_TYPES, PathwayClosedError, type PathwayScope } from "./pathway.js";
import { defaultRetryOn, type RetryStrategy, type RetryOutcome } from "./retry.js";
import { Engram, type ImprintOp, type ImprintReceipt, type RecallMode, type RecallResult } from "./engram.js";
import { EngramClient } from "./engram-client.js";
import { imprintedSignal, recalledSignal } from "./signals.js";
import { ambientTrace } from "./trace-context.js";
import { newEventId, type Directed } from "./envelope.js";
import type { MessageHandler, Subscription, Synapse } from "./synapse.js";
import {
  neuronRecord,
  type ListOptions,
  type NeuronRecord,
  type NeuronStatus,
  type RegistryStore,
} from "./storage.js";

// --- explicit resource management (`await using`) ------------------------
declare global {
  interface SymbolConstructor {
    readonly asyncDispose: unique symbol;
  }
}
(Symbol as { asyncDispose?: symbol }).asyncDispose ??= Symbol.for("Symbol.asyncDispose");

export type SignalHandler = (signal: Signal) => void | Promise<void>;

/** Optional narrowing for handler registration  -  the TS counterpart to the
 *  Python decorators' `neuron=` / `capability=` / `trace_id=` kwargs. */
export interface HandlerFilter {
  neuron?: string;
  capability?: string;
  traceId?: string;
}

/** Raised when an emit violates the protocol (e.g. emitting an Axon-only type). */
export class DendriteProtocolError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "DendriteProtocolError";
  }
}
export { DendriteProtocolError as CortexProtocolError };

export type DendriteRole = "orchestrator" | "worker";

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
  /** "orchestrator" (default, may dispatch TASKs) or "worker" (hosts Axons;
   *  TASK initiation is refused, everything else is role-agnostic). */
  role?: DendriteRole;
  /** Default bidder (default true): a Dendrite hosting Axons answers
   *  TASK_OFFERs whose capability set a hosted Axon covers, with cost 0 /
   *  confidence 1  -  unless a user onTaskOffer handler is registered, which
   *  suppresses the default bidder entirely. */
  autoBid?: boolean;
  /** Liveness: a registered Neuron whose last heartbeat is older than this is
   *  marked deregistered by the heartbeat loop's sweep. Default: 3 heartbeat
   *  intervals; 0 disables. */
  staleAfterMs?: number;
}

interface DispatchArgs {
  neuron?: string;
  input: Json;
  traceId?: string;
  parentId?: string | null;
  contextRef?: string;
  capabilities?: string[];
  meta?: Json;
}

export class Dendrite {
  readonly synapse: Synapse;
  readonly registryStore: RegistryStore | null;
  readonly namespace: string;
  readonly dendriteId: string;
  readonly role: DendriteRole;
  private readonly heartbeatMs: number;
  private readonly reregisterOnHeartbeat: boolean;
  private readonly autoBid: boolean;
  private readonly staleAfterMs: number;

  private readonly _axons = new Map<string, Axon>();
  private readonly handlers = new Map<SignalType, SignalHandler[]>();
  private taskSub: Subscription | null = null;
  private routedTaskSub: Subscription | null = null;
  private readonly inboundSubs = new Map<SignalType, Subscription>();
  private readonly inflightSubs = new Map<SignalType, Promise<void>>();
  private readonly pendingSubs = new Set<Promise<void>>();
  /** Recently seen CLARIFICATION_ANSWER / PERMISSION_DECISION signals keyed
   *  by parent_id, so {@link awaitDecision} can serve an answer that arrived
   *  before it was called (an in-process synapse can deliver the whole
   *  request->answer chain within the original publish). Bounded FIFO. */
  private readonly recentDecisions = new Map<string, Signal>();

  /** Hosted Engrams keyed by engramId, plus a kind index so RECALL/IMPRINT
   *  addressed by engramKind reach every matching host. */
  private readonly _engrams = new Map<string, Engram>();
  // In-flight neuron work keyed by trace_id so a STOP can abandon exactly
  // one workflow. JS can't force-kill a running async body, so abort means
  // 'stop awaiting + suppress the reply'; the neuron should also check the
  // AbortSignal cooperatively where it can.
  private readonly traceAborts = new Map<string, Set<AbortController>>();
  private readonly engramKindIndex = new Map<string, string[]>();
  /** Engrams learned from peer REGISTER signals (possibly out-of-process). */
  private readonly _engramRegistrations = new Map<string, Directed>();
  private readonly engramRegKindIndex = new Map<string, Set<string>>();
  /** Caller-side correlation table for RECALL/IMPRINT awaiting
   *  RECALLED/IMPRINTED. The Dendrite owns the subscriptions and feeds it. */
  readonly engramClient: EngramClient = new EngramClient(this);
  private heartbeatTimer: ReturnType<typeof setTimeout> | null = null;
  private heartbeatStopped = true;
  private running = false;

  /** Open Pathways keyed by trace_id (dispatch / observePathway). */
  private readonly pathways = new Map<string, Pathway>();
  /** Per-operation Pathways keyed by the issuing request's id (matched
   *  against inbound parent_id)  -  the generic request/reply primitive
   *  behind awaitDecision (and a future EngramClient wiring). */
  private readonly opPathways = new Map<string, Pathway>();

  /** @internal  -  lifecycle hooks for this Dendrite. */
  readonly hooks: LifecycleHooks<Dendrite> = new LifecycleHooks<Dendrite>(this);

  constructor(opts: DendriteOptions) {
    if (!opts.synapse) throw new TypeError("Dendrite requires a synapse");
    const role = opts.role ?? "orchestrator";
    if (role !== "orchestrator" && role !== "worker") {
      throw new Error(`role must be 'orchestrator' or 'worker', got '${role as string}'`);
    }
    this.synapse = opts.synapse;
    this.registryStore = opts.registryStore ?? null;
    this.namespace = opts.namespace ?? "default";
    this.dendriteId = opts.dendriteId ?? "dendrite";
    this.heartbeatMs = opts.heartbeatMs ?? 30_000;
    this.reregisterOnHeartbeat = opts.reregisterOnHeartbeat ?? true;
    this.role = role;
    this.autoBid = opts.autoBid ?? true;
    this.staleAfterMs =
      opts.staleAfterMs ?? (this.heartbeatMs > 0 ? this.heartbeatMs * 3 : 0);
    // Handlers are keyed by every SignalType so the full decorator surface
    // (onPlan, onFinal, onBid, ...) can attach without registration errors.
    for (const t of Object.values(SignalType) as SignalType[]) {
      this.handlers.set(t, []);
    }
  }

  // -- properties ----------------------------------------------------

  get axons(): ReadonlyMap<string, Axon> {
    return new Map(this._axons);
  }

  axon(neuronId: string): Axon | undefined {
    return this._axons.get(neuronId);
  }

  /** Aggregate of every attached Axon's capabilities, deduplicated + sorted. */
  get capabilities(): string[] {
    const caps = new Set<string>();
    for (const ax of this._axons.values()) for (const c of ax.capabilities) caps.add(c);
    return [...caps].sort();
  }

  /** Canonical queue-group name for this Dendrite's aggregate caps, or null
   *  when no Axons are attached. Identical Dendrites share a group. */
  private capQueueGroup(): string | null {
    const caps = this.capabilities;
    return caps.length ? `caps:${caps.join(",")}` : null;
  }

  private requireOrchestrator(op: string): void {
    if (this.role !== "orchestrator") {
      throw new DendriteProtocolError(
        `Dendrite role='${this.role}' cannot perform '${op}': only ` +
          `role='orchestrator' Dendrites may dispatch TASK signals. Workers ` +
          `host Axons and emit replies / cognition signals freely.`,
      );
    }
  }

  // -- attachment ----------------------------------------------------

  /**
   * Attach an Axon to a *stopped* Dendrite. Throws if the Dendrite is
   * running  -  a running Dendrite needs the async activation path
   * (subscriptions, queue-group refresh, REGISTER): use
   * `await dendrite.addAxon(axon)` instead, which works in both states.
   */
  attachAxon(axon: Axon): void {
    if (this.running) {
      throw new Error(
        "attachAxon on a running Dendrite would never receive TASKs (no " +
          "subscription / REGISTER is set up after start). Use " +
          "`await dendrite.addAxon(axon)` instead.",
      );
    }
    this.attachAxonRecord(axon);
  }

  private attachAxonRecord(axon: Axon): void {
    if (this._axons.has(axon.neuronId)) {
      throw new Error(`Dendrite already has an Axon for neuronId='${axon.neuronId}'`);
    }
    this._axons.set(axon.neuronId, axon);
    axon[ATTACH](this);
  }

  /**
   * Attach an Axon; if the Dendrite is running, activate it live: ensure the
   * addressed + routed TASK subscriptions exist (re-keying the routed queue
   * group for the new aggregate cap profile), subscribe TASK_AWARDED /
   * DISCOVER (and TASK_OFFER when autoBid), mirror to the registry store,
   * emit REGISTER, and fire the Axon's onConnect hooks.
   */
  async addAxon(axon: Axon): Promise<void> {
    this.attachAxonRecord(axon);
    if (!this.running) return;
    if (this.taskSub === null) {
      this.taskSub = await this.synapse.subscribe(
        this.subject(SignalType.TASK),
        (s) => this.onTask(s),
      );
    }
    await this.refreshRoutedSub();
    await this.ensureInboundSub(SignalType.TASK_AWARDED);
    await this.ensureInboundSub(SignalType.DISCOVER);
    if (this.autoBid) await this.ensureInboundSub(SignalType.TASK_OFFER);
    await this.mirrorToStore(axon, "registered");
    await this.emitRegister(axon);
    await axon.hooks._fireConnect();
    axon.hooks._launchSchedule();
  }

  /** Detach an Axon. If running: deregister, tear down its hooks, and re-key
   *  (or drop) the TASK subscriptions for the changed cap profile. */
  async detachAxon(neuronId: string, opts: { reason?: string } = {}): Promise<void> {
    const axon = this._axons.get(neuronId);
    if (!axon) {
      throw new Error(`Dendrite has no Axon for neuronId='${neuronId}'`);
    }
    if (this.running) {
      axon.hooks._stopHooks();
      if (this.registryStore !== null) {
        try {
          await this.registryStore.markDeregistered(neuronId);
        } catch {
          /* best-effort */
        }
      }
      await this.emitDeregister(axon, opts.reason);
    }
    this._axons.delete(neuronId);
    axon[DETACH]();

    if (this.running && this._axons.size === 0) {
      if (this.taskSub !== null) {
        try {
          await this.taskSub.unsubscribe();
        } catch {
          /* best-effort */
        }
        this.taskSub = null;
      }
      if (this.routedTaskSub !== null) {
        try {
          await this.routedTaskSub.unsubscribe();
        } catch {
          /* best-effort */
        }
        this.routedTaskSub = null;
      }
    } else if (this.running) {
      // Axons remain: the aggregate cap profile changed  -  re-key the group.
      await this.refreshRoutedSub();
    }
  }

  /**
   * Mount an Engram on this Dendrite. After attachment (and start), the
   * Dendrite subscribes to RECALL/IMPRINT, routes Signals addressed to
   * `engram.engramId` or matching `engram.engramKind` to the instance, and
   * announces it on the Synapse with an engram REGISTER. The Engram still
   * owns its backend lifecycle: `connect()` on start(), `close()` on stop().
   * When the Dendrite is already running, the backend is connected and the
   * subscriptions/REGISTER are established immediately.
   */
  async attachEngram(engram: Engram): Promise<void> {
    if (this._engrams.has(engram.engramId)) {
      throw new Error(`Dendrite already hosts an Engram with engramId='${engram.engramId}'`);
    }
    this._engrams.set(engram.engramId, engram);
    const bucket = this.engramKindIndex.get(engram.engramKind) ?? [];
    bucket.push(engram.engramId);
    this.engramKindIndex.set(engram.engramKind, bucket);
    if (this.running) {
      await engram.connect();
      await this.ensureInboundSub(SignalType.RECALL);
      await this.ensureInboundSub(SignalType.IMPRINT);
      await this.ensureInboundSub(SignalType.REGISTER);
      await this.emitEngramRegister(engram);
    }
  }

  /** Remove a hosted Engram. Closes its backend if the Dendrite is running. */
  async detachEngram(engramId: string): Promise<void> {
    const engram = this._engrams.get(engramId);
    if (!engram) {
      throw new Error(`Dendrite has no Engram with engramId='${engramId}'`);
    }
    if (this.running) {
      try {
        await engram.close();
      } catch {
        /* best-effort teardown */
      }
    }
    const bucket = this.engramKindIndex.get(engram.engramKind) ?? [];
    const kept = bucket.filter((id) => id !== engramId);
    if (kept.length) this.engramKindIndex.set(engram.engramKind, kept);
    else this.engramKindIndex.delete(engram.engramKind);
    this._engrams.delete(engramId);
  }

  get engrams(): ReadonlyMap<string, Engram> {
    return new Map(this._engrams);
  }

  /** Engrams learned via REGISTER, keyed by directed.id (or directed.type
   *  when no id), including in-process ones. */
  get engramRegistrations(): ReadonlyMap<string, Directed> {
    return new Map(this._engramRegistrations);
  }

  /** True when an Engram with this id/kind is reachable  -  hosted
   *  in-process or learned from a peer's REGISTER. */
  isEngramKnown(opts: { engramId?: string; engramKind?: string }): boolean {
    if (opts.engramId) {
      if (this._engrams.has(opts.engramId) || this._engramRegistrations.has(opts.engramId)) {
        return true;
      }
    }
    if (opts.engramKind) {
      if (this.engramKindIndex.has(opts.engramKind) || this.engramRegKindIndex.has(opts.engramKind)) {
        return true;
      }
    }
    return false;
  }

  /** (Re)subscribe the capability-routed TASK subscription so its queue
   *  group matches the *current* aggregate cap profile. */
  private async refreshRoutedSub(): Promise<void> {
    const qgroup = this.capQueueGroup();
    if (this.routedTaskSub !== null) {
      try {
        await this.routedTaskSub.unsubscribe();
      } catch {
        /* best-effort */
      }
      this.routedTaskSub = null;
    }
    if (qgroup !== null) {
      this.routedTaskSub = await this.synapse.subscribe(
        this.routedSubject(),
        (s) => this.onTask(s),
        { queueGroup: qgroup },
      );
    }
  }

  // -- inbound handler registration ----------------------------------

  private wrapWithFilter(fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    if (!filter || (filter.neuron === undefined && filter.capability === undefined && filter.traceId === undefined)) {
      return fn;
    }
    return async (sig: Signal): Promise<void> => {
      const sigNeuron = sig.directed?.id ?? null;
      if (filter.neuron !== undefined && sigNeuron !== filter.neuron) return;
      if (filter.traceId !== undefined && sig.trace_id !== filter.traceId) return;
      if (filter.capability !== undefined) {
        if (!(await this.neuronHasCapability(sigNeuron, filter.capability))) return;
      }
      await fn(sig);
    };
  }

  private async neuronHasCapability(
    neuronId: string | null,
    capability: string,
  ): Promise<boolean> {
    if (!neuronId) return false;
    const axon = this._axons.get(neuronId);
    if (axon) return axon.capabilities.includes(capability);
    if (this.registryStore !== null) {
      try {
        const recs = await this.registryStore.list({ includeDeregistered: true });
        const rec = recs.find((r) => r.neuron_id === neuronId);
        return rec ? rec.capabilities.includes(capability) : false;
      } catch {
        return false;
      }
    }
    return false;
  }

  private on(type: SignalType, fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    const list = this.handlers.get(type)!;
    list.push(this.wrapWithFilter(fn, filter));
    if (this.running && !this.inboundSubs.has(type)) {
      // Late registration: track the async subscription so failures are
      // observable and ensureSubscribed() can await completion.
      const p = this.ensureInboundSub(type).finally(() => this.pendingSubs.delete(p));
      this.pendingSubs.add(p);
      p.catch(() => {
        /* surfaced via ensureSubscribed(); never an unhandled rejection */
      });
    }
    return fn;
  }

  /**
   * Generic handler registration for *any* SignalType  -  the escape hatch
   * behind every named `on*` helper. New protocol types are observable the
   * day they exist. Supports the same filters as the named helpers.
   */
  onSignal(type: SignalType, fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    return this.on(type, fn, filter);
  }

  /** Await until inbound subscriptions exist for `types`  -  removes the
   *  late-registration race deterministically. Idempotent. */
  async ensureSubscribed(...types: SignalType[]): Promise<void> {
    for (const t of types) await this.ensureInboundSub(t);
  }

  // -- lifecycle / reply handlers --
  onAgentOutput(fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    return this.on(SignalType.AGENT_OUTPUT, fn, filter);
  }
  onClarification(fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    return this.on(SignalType.CLARIFICATION, fn, filter);
  }
  /**
   * Register a handler fired on inbound PERMISSION requests - the *answering*
   * side. Reply via {@link respondToPermission} (re-dispatch a TASK with the
   * verdict) or {@link grantPermission} / {@link denyPermission} (emit a
   * discrete PERMISSION_DECISION).
   */
  onPermission(fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    return this.on(SignalType.PERMISSION, fn, filter);
  }
  onErrorSignal(fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    return this.on(SignalType.ERROR, fn, filter);
  }
  /** Register a handler fired on FINAL  -  workflow conclusion. */
  onFinal(fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    return this.on(SignalType.FINAL, fn, filter);
  }
  /** Observe inbound TASKs (audit/logging). Observation only  -  Axon routing
   *  happens on its own subscription and is unaffected. */
  onTaskSignal(fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    return this.on(SignalType.TASK, fn, filter);
  }
  onRegister(fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    return this.on(SignalType.REGISTER, fn, filter);
  }
  onDeregister(fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    return this.on(SignalType.DEREGISTER, fn, filter);
  }
  onHeartbeat(fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    return this.on(SignalType.HEARTBEAT, fn, filter);
  }
  onDiscover(fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    return this.on(SignalType.DISCOVER, fn, filter);
  }

  // -- cognition handlers --
  onPlan(fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    return this.on(SignalType.PLAN, fn, filter);
  }
  onThoughtDelta(fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    return this.on(SignalType.THOUGHT_DELTA, fn, filter);
  }
  onToolCall(fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    return this.on(SignalType.TOOL_CALL, fn, filter);
  }
  onToolResult(fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    return this.on(SignalType.TOOL_RESULT, fn, filter);
  }
  onMemoryAppend(fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    return this.on(SignalType.MEMORY_APPEND, fn, filter);
  }
  onCritique(fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    return this.on(SignalType.CRITIQUE, fn, filter);
  }
  onEscalation(fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    return this.on(SignalType.ESCALATION, fn, filter);
  }
  onConsensus(fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    return this.on(SignalType.CONSENSUS, fn, filter);
  }
  onContextSync(fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    return this.on(SignalType.CONTEXT_SYNC, fn, filter);
  }

  // -- routing / market handlers --
  /** Workers use this to evaluate offers and call {@link bid} to compete.
   *  Registering it suppresses the default auto-bidder entirely. */
  onTaskOffer(fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    return this.on(SignalType.TASK_OFFER, fn, filter);
  }
  /** Observe BIDs (market observability). dispatchOffer collects its own. */
  onBid(fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    return this.on(SignalType.BID, fn, filter);
  }
  /** Observe TASK_AWARDED. The hosting Dendrite's award-to-TASK synthesis is
   *  unaffected by handlers here. */
  onTaskAwarded(fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    return this.on(SignalType.TASK_AWARDED, fn, filter);
  }
  /** e.g. release a reservation made while bidding. */
  onTaskDeclined(fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    return this.on(SignalType.TASK_DECLINED, fn, filter);
  }

  // -- discrete answer-path consumers --
  /** Fired on CLARIFICATION_ANSWER  -  correlate by `sig.parent_id === the
   *  CLARIFICATION's id`, or use {@link awaitDecision}. */
  onClarificationAnswer(fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    return this.on(SignalType.CLARIFICATION_ANSWER, fn, filter);
  }
  /** Fired on PERMISSION_DECISION  -  correlate by parent_id, or use
   *  {@link awaitDecision}. */
  onPermissionDecision(fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    return this.on(SignalType.PERMISSION_DECISION, fn, filter);
  }

  // -- memory-traffic observers --
  onRecalled(fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    return this.on(SignalType.RECALLED, fn, filter);
  }
  onImprinted(fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    return this.on(SignalType.IMPRINTED, fn, filter);
  }
  onRecallSignal(fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    return this.on(SignalType.RECALL, fn, filter);
  }
  onImprintSignal(fn: SignalHandler, filter?: HandlerFilter): SignalHandler {
    return this.on(SignalType.IMPRINT, fn, filter);
  }

  // -- trace-scoped helper --
  private static readonly TRACE_DEFAULT_TYPES: readonly SignalType[] = [
    SignalType.AGENT_OUTPUT,
    SignalType.FINAL,
    SignalType.ERROR,
    SignalType.PLAN,
    SignalType.THOUGHT_DELTA,
    SignalType.TOOL_CALL,
    SignalType.TOOL_RESULT,
    SignalType.MEMORY_APPEND,
    SignalType.CRITIQUE,
    SignalType.ESCALATION,
    SignalType.CONSENSUS,
    SignalType.CONTEXT_SYNC,
    SignalType.CLARIFICATION,
  ];

  /** Register one handler for multiple types narrowed to a single workflow. */
  onTrace(traceId: string, fn: SignalHandler, types?: SignalType[]): SignalHandler {
    for (const t of types ?? Dendrite.TRACE_DEFAULT_TYPES) {
      this.on(t, fn, { traceId });
    }
    return fn;
  }

  // -- lifecycle hooks ----------------------------------------------

  onConnect(fn: ConnectHook<Dendrite>): ConnectHook<Dendrite> {
    return this.hooks.onConnect(fn);
  }
  onRefresh(fn: RefreshHook<Dendrite>): RefreshHook<Dendrite> {
    return this.hooks.onRefresh(fn);
  }
  onSchedule(everyMs: number, fn: ScheduleHook<Dendrite>): ScheduleHook<Dendrite> {
    return this.hooks.onSchedule(everyMs, fn);
  }
  async refresh(
    opts: { reason?: string; neuronId?: string | null; extra?: Record<string, unknown> } = {},
  ): Promise<void> {
    await this.hooks.refresh(opts);
  }

  // -- lifecycle -----------------------------------------------------

  async start(): Promise<void> {
    if (this.running) return;

    if (this.registryStore !== null) await this.registryStore.connect();

    if (this._axons.size > 0) {
      // Two TASK subscriptions for two routing modes: addressed (broadcast,
      // every Dendrite filters by neuron_id) and capability-routed
      // (queue-grouped on the aggregate cap signature  -  consumed once per
      // matching group).
      this.taskSub = await this.synapse.subscribe(
        this.subject(SignalType.TASK),
        (s) => this.onTask(s),
      );
      const qgroup = this.capQueueGroup();
      if (qgroup !== null) {
        this.routedTaskSub = await this.synapse.subscribe(
          this.routedSubject(),
          (s) => this.onTask(s),
          { queueGroup: qgroup },
        );
      }
      await this.ensureInboundSub(SignalType.TASK_AWARDED);
      await this.ensureInboundSub(SignalType.DISCOVER);
      await this.ensureInboundSub(SignalType.STOP);
      if (this.autoBid) await this.ensureInboundSub(SignalType.TASK_OFFER);
      for (const axon of this._axons.values()) {
        await this.mirrorToStore(axon, "registered");
        await this.emitRegister(axon);
      }
    }

    // Engram hosting: connect backends, listen for RECALL/IMPRINT, announce
    // each hosted Engram with an engram REGISTER, and learn peer Engrams.
    if (this._engrams.size > 0) {
      for (const engram of this._engrams.values()) {
        try {
          await engram.connect();
        } catch {
          /* backend connect failures are logged by the backend; keep going */
        }
      }
      await this.ensureInboundSub(SignalType.RECALL);
      await this.ensureInboundSub(SignalType.IMPRINT);
      // Terminal events are the saga commit point - an Engram host must see
      // FINAL/ERROR (and STOP, below) to commit or roll back its journal even
      // when it never dispatches.
      await this.ensureInboundSub(SignalType.FINAL);
      await this.ensureInboundSub(SignalType.ERROR);
      await this.ensureInboundSub(SignalType.REGISTER);
      for (const engram of this._engrams.values()) {
        try {
          await this.emitEngramRegister(engram);
        } catch {
          /* best-effort announce */
        }
      }
    }
    // Always listen for RECALLED/IMPRINTED  -  the Dendrite owns the
    // EngramClient's correlation table even when it hosts no Axons, because
    // an orchestrator calls dendrite.recall/imprint directly.
    await this.ensureInboundSub(SignalType.RECALLED);
    await this.ensureInboundSub(SignalType.IMPRINTED);

    for (const [type, hs] of this.handlers) {
      if (hs.length) await this.ensureInboundSub(type);
    }

    if (this.registryStore !== null) {
      for (const t of [SignalType.REGISTER, SignalType.DEREGISTER, SignalType.HEARTBEAT]) {
        await this.ensureInboundSub(t);
      }
    }

    // Every started Dendrite listens for STOP so it can cancel its share of
    // any trace it participates in.
    await this.ensureInboundSub(SignalType.STOP);

    this.running = true;

    if (this._axons.size > 0 && this.heartbeatMs > 0) {
      this.startHeartbeatLoop();
    }

    await this.hooks._fireConnect();
    this.hooks._launchSchedule();
    for (const axon of this._axons.values()) {
      await axon.hooks._fireConnect();
      axon.hooks._launchSchedule();
    }
  }

  private startHeartbeatLoop(): void {
    this.heartbeatStopped = false;

    const schedule = (): void => {
      this.heartbeatTimer = setTimeout(() => {
        void tick();
      }, this.heartbeatMs);
      (this.heartbeatTimer as { unref?: () => void }).unref?.();
    };

    const tick = async (): Promise<void> => {
      if (this.heartbeatStopped || !this.running) return;
      try {
        await this.heartbeatTick();
      } catch {
        /* backstop: a throw must never kill the loop */
      }
      if (!this.heartbeatStopped && this.running) schedule();
    };

    schedule();
  }

  async stop(reason?: string): Promise<void> {
    // Close open Pathways FIRST so awaiters don't hang and iteration sees the
    // close sentinel cleanly; same for in-flight op-Pathways.
    for (const pw of [...this.pathways.values()]) {
      try {
        await pw.close();
      } catch {
        /* best-effort */
      }
    }
    this.pathways.clear();
    for (const pw of [...this.opPathways.values()]) {
      try {
        await pw.close();
      } catch {
        /* best-effort */
      }
    }
    this.opPathways.clear();

    // Cancel in-flight engram I/O so awaiters wake with EngramCancelled
    // instead of hanging on a deadline.
    this.engramClient.cancelAll();

    if (!this.running) return;
    this.running = false;

    for (const engram of this._engrams.values()) {
      try {
        await engram.close();
      } catch {
        /* best-effort teardown */
      }
    }

    this.hooks._stopHooks();
    for (const axon of this._axons.values()) axon.hooks._stopHooks();

    this.heartbeatStopped = true;
    if (this.heartbeatTimer !== null) {
      clearTimeout(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }

    if (this.taskSub !== null) {
      await this.taskSub.unsubscribe();
      this.taskSub = null;
    }
    if (this.routedTaskSub !== null) {
      await this.routedTaskSub.unsubscribe();
      this.routedTaskSub = null;
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
  }

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
  async registrySnapshot(opts: ListOptions & { maxAgeMs?: number } = {}): Promise<NeuronRecord[]> {
    const { maxAgeMs, ...listOpts } = opts;
    const records = await this.requireStore().list(listOpts);
    return maxAgeMs !== undefined ? Dendrite.filterFresh(records, maxAgeMs) : records;
  }

  /** Live (non-deregistered) records, optionally filtered by capability.
   *  `maxAgeMs` additionally drops records whose last heartbeat is older  -
   *  a read-side freshness guard when the background sweep can't be relied on. */
  async findNeurons(opts: { capability?: string; maxAgeMs?: number } = {}): Promise<NeuronRecord[]> {
    const records = await this.requireStore().list({
      ...(opts.capability !== undefined ? { capability: opts.capability } : {}),
      includeDeregistered: false,
    });
    return opts.maxAgeMs !== undefined
      ? Dendrite.filterFresh(records, opts.maxAgeMs)
      : records;
  }

  private static filterFresh(records: NeuronRecord[], maxAgeMs: number): NeuronRecord[] {
    const now = Date.now();
    return records.filter((r) => {
      const seen = r.last_heartbeat ?? r.registered_at;
      if (!seen) return false;
      return now - Date.parse(seen) <= maxAgeMs;
    });
  }

  // -- outbound primitives ------------------------------------------

  /**
   * Emit a TASK. Addressed (`neuron`) or capability-routed (`capabilities`)
   *  -  at least one must be set. `finalize: true` tags the TASK so the
   * handling worker Dendrite promotes a successful AGENT_OUTPUT to FINAL
   * (terminal-handler finalize  -  see {@link dispatch}). Only
   * orchestrator-role Dendrites may dispatch.
   */
  async dispatchTask(args: DispatchArgs & { finalize?: boolean }): Promise<Signal> {
    this.requireOrchestrator("dispatchTask");
    if (!args.neuron && !args.capabilities?.length) {
      throw new Error(
        "dispatchTask requires either neuron (addressed) or capabilities " +
          "(capability-routed)",
      );
    }
    const sig = taskSignal({
      input: args.input,
      ...(args.neuron ? { directed: { id: args.neuron } } : {}),
      ...(args.traceId !== undefined ? { traceId: args.traceId } : {}),
      ...(args.parentId !== undefined ? { parentId: args.parentId } : {}),
      ...(args.contextRef !== undefined ? { contextRef: args.contextRef } : {}),
      ...(args.capabilities !== undefined ? { capabilities: args.capabilities } : {}),
      ...(args.finalize !== undefined ? { finalize: args.finalize } : {}),
      ...(args.meta !== undefined ? { meta: args.meta } : {}),
    });
    await this.publishTask(sig);
    return sig;
  }

  /** Publish a TASK to the right subject for its routing mode. Addressed ->
   *  broadcast subject; capability-routed -> queue-grouped routed subject. */
  private async publishTask(sig: Signal): Promise<void> {
    const subject =
      sig.directed?.id
        ? this.subject(SignalType.TASK)
        : (sig.payload["capabilities"] as string[] | undefined)?.length
          ? this.routedSubject()
          : this.subject(SignalType.TASK);
    await this.synapse.publish(subject, sig);
  }

  // -- Pathway-based dispatch ----------------------------------------

  /**
   * Dispatch a TASK and return a {@link Pathway} scoped to its trace  -
   * await it, attach callbacks, or iterate:
   *
   * ```ts
   * // 1) sequential / request-reply
   * const pw = await orch.dispatch({ neuron: "summarize", input });
   * const out = await pw.wait();
   *
   * // 2) reactive
   * pw.on(SignalType.PLAN, (sig) => { ... });
   *
   * // 3) streaming
   * for await (const sig of pw) { ... }
   * ```
   *
   * `capabilities` instead of `neuron` gives event-driven dispatch. Delivery
   * is exactly-once within a queue group (identical cap profiles) but
   * **at-least-once across heterogeneous groups**  -  use
   * {@link dispatchOffer} when overlapping profiles need an atomic claim.
   *
   * `scope: "terminal"` filters delivery to FINAL / ERROR / CLARIFICATION /
   * PERMISSION. `finalize` (default: true exactly when scope is "terminal")
   * tags the TASK for terminal-handler finalize: the worker Dendrite promotes
   * a successful AGENT_OUTPUT by also emitting FINAL  -  a default Axon never
   * emits FINAL itself, so a terminal-scoped Pathway would otherwise never
   * resolve against stock workers.
   */
  async dispatch(
    args: DispatchArgs & { scope?: PathwayScope; finalize?: boolean },
  ): Promise<Pathway> {
    this.requireOrchestrator("dispatch");
    if (!args.neuron && !args.capabilities?.length) {
      throw new Error(
        "dispatch requires either neuron (addressed) or capabilities " +
          "(capability-routed)",
      );
    }
    const scope = args.scope ?? "all";
    const finalize = args.finalize ?? scope === "terminal";
    const tid = args.traceId ?? newTraceId();

    await this.ensurePathwaySubs();

    // Register the Pathway BEFORE emitting so a fast-path response finds it.
    const pathway = new Pathway({
      traceId: tid,
      role: "originator",
      scope,
      onClose: (pw) => {
        this.pathways.delete(pw.traceId);
      },
    });
    this.pathways.set(tid, pathway);

    const sig = taskSignal({
      input: args.input,
      traceId: tid,
      ...(args.neuron ? { directed: { id: args.neuron } } : {}),
      ...(args.parentId !== undefined ? { parentId: args.parentId } : {}),
      ...(args.contextRef !== undefined ? { contextRef: args.contextRef } : {}),
      ...(args.capabilities !== undefined ? { capabilities: args.capabilities } : {}),
      finalize,
      ...(args.meta !== undefined ? { meta: args.meta } : {}),
    });
    try {
      await this.publishTask(sig);
    } catch (err) {
      this.pathways.delete(tid);
      await pathway.close();
      throw err;
    }
    return pathway;
  }

  /** Sync-shape sugar: dispatch, await the first matching Signal, close the
   *  Pathway, return the Signal. Use `scope: "terminal"` to wait only for
   *  FINAL / ERROR / CLARIFICATION / PERMISSION. */
  async dispatchAndWait(
    args: DispatchArgs & {
      scope?: PathwayScope;
      finalize?: boolean;
      timeoutMs?: number;
      retry?: RetryStrategy;
    },
  ): Promise<Signal> {
    const { timeoutMs, retry, ...rest } = args;
    if (retry) {
      return this.runWithRetry({ ...rest, retry, ...(timeoutMs !== undefined ? { timeoutMs } : {}) });
    }
    const pathway = await this.dispatch(rest);
    try {
      return await pathway.wait(timeoutMs ?? 30_000);
    } finally {
      await pathway.close();
    }
  }

  /** Async-shape sugar: dispatch, return the live Pathway immediately. The
   *  caller attaches `pw.on(...)` callbacks or iterates. */
  async dispatchAndSubscribe(
    args: DispatchArgs & { scope?: PathwayScope; finalize?: boolean },
  ): Promise<Pathway> {
    return this.dispatch(args);
  }

  /** Open a Pathway in *observer* role for a trace another peer started. */
  async observePathway(traceId: string): Promise<Pathway> {
    if (this.pathways.has(traceId)) {
      throw new Error(`Dendrite already has a Pathway open for trace '${traceId}'`);
    }
    await this.ensurePathwaySubs();
    const pathway = new Pathway({
      traceId,
      role: "observer",
      onClose: (pw) => {
        this.pathways.delete(pw.traceId);
      },
    });
    this.pathways.set(traceId, pathway);
    return pathway;
  }

  private async ensurePathwaySubs(): Promise<void> {
    for (const t of PATHWAY_TYPES) await this.ensureInboundSub(t);
  }

  // -- per-operation (request/reply) Pathways -------------------------

  private openOpPathway(opId: string, traceId: string): Pathway {
    const pathway = new Pathway({
      traceId,
      parentId: opId,
      role: "originator",
      onClose: (pw) => {
        if (pw.parentId !== null) this.opPathways.delete(pw.parentId);
      },
    });
    this.opPathways.set(opId, pathway);
    return pathway;
  }

  private async cancelOpPathways(traceId: string): Promise<void> {
    for (const pw of [...this.opPathways.values()].filter((p) => p.traceId === traceId)) {
      try {
        await pw.close();
      } catch {
        /* teardown must not throw */
      }
    }
  }

  /**
   * Await the discrete answer to a CLARIFICATION or PERMISSION request.
   *
   * Opens a per-operation Pathway keyed on `request.id` and resolves on the
   * first CLARIFICATION_ANSWER / PERMISSION_DECISION whose parent_id matches.
   * The awaitable counterpart to {@link onClarificationAnswer} /
   * {@link onPermissionDecision}.
   */
  async awaitDecision(request: Signal, opts: { timeoutMs?: number } = {}): Promise<Signal> {
    let expected: SignalType;
    if (request.type === SignalType.CLARIFICATION) {
      expected = SignalType.CLARIFICATION_ANSWER;
    } else if (request.type === SignalType.PERMISSION) {
      expected = SignalType.PERMISSION_DECISION;
    } else {
      throw new DendriteProtocolError(
        `awaitDecision expects a CLARIFICATION or PERMISSION signal, got '${request.type}'`,
      );
    }
    await this.ensureInboundSub(expected);
    // The answer may already have flown by (in-process synapses deliver the
    // whole request->answer chain inside the original publish). Serve and
    // consume the cached copy if so.
    const cached = this.recentDecisions.get(request.id);
    if (cached && cached.type === expected) {
      this.recentDecisions.delete(request.id);
      return cached;
    }
    const pathway = this.openOpPathway(request.id, request.trace_id);
    try {
      return await pathway.waitFor(expected, opts.timeoutMs ?? 30_000);
    } finally {
      await pathway.close();
    }
  }

  // -- competitive bidding: TASK_OFFER / BID / TASK_AWARDED ------------

  /**
   * Broadcast a TASK_OFFER, collect BIDs for `deadlineMs`, award the winner
   * per `select` ("first_bid" | "lowest_cost" | "highest_confidence"), and
   * return a Pathway scoped to the resulting workflow. Losers get
   * TASK_DECLINED. Throws a TimeoutError-named Error when no BID arrives.
   * `finalize` follows the same rule as {@link dispatch}.
   */
  async dispatchOffer(args: {
    input: Json;
    capabilities?: string[];
    deadlineMs?: number;
    select?: "first_bid" | "lowest_cost" | "highest_confidence";
    traceId?: string;
    parentId?: string | null;
    contextRef?: string;
    meta?: Json;
    scope?: PathwayScope;
    finalize?: boolean;
  }): Promise<Pathway> {
    this.requireOrchestrator("dispatchOffer");
    const select = args.select ?? "first_bid";
    if (!["first_bid", "lowest_cost", "highest_confidence"].includes(select)) {
      throw new Error(
        `select must be 'first_bid' / 'lowest_cost' / 'highest_confidence', got '${select}'`,
      );
    }
    const deadlineMs = args.deadlineMs ?? 250;
    const scope = args.scope ?? "all";
    const finalize = args.finalize ?? scope === "terminal";
    const tid = args.traceId ?? newTraceId();

    await this.ensurePathwaySubs();
    await this.ensureInboundSub(SignalType.BID);

    const pathway = new Pathway({
      traceId: tid,
      role: "originator",
      scope,
      onClose: (pw) => {
        this.pathways.delete(pw.traceId);
      },
    });
    this.pathways.set(tid, pathway);

    const offer = taskOfferSignal({
      traceId: tid,
      input: args.input,
      deadlineMs,
      ...(args.parentId !== undefined ? { parentId: args.parentId } : {}),
      ...(args.capabilities !== undefined ? { capabilities: args.capabilities } : {}),
      ...(args.meta !== undefined ? { meta: args.meta } : {}),
    });

    const bids: Signal[] = [];
    let firstBid: ((s: Signal) => void) | null = null;
    const firstBidArrived = new Promise<Signal | null>((resolve) => {
      firstBid = resolve;
    });
    pathway.on(SignalType.BID, (sig) => {
      bids.push(sig);
      if (select === "first_bid") firstBid?.(sig);
    });

    try {
      await this.emit(offer);
    } catch (err) {
      this.pathways.delete(tid);
      await pathway.close();
      throw err;
    }

    // Wait out the bidding window (first_bid short-circuits).
    // Deliberately NOT unref'd: the bidding window must keep the event
    // loop alive for its bounded duration.
    const sleep = (ms: number): Promise<null> => new Promise((r) => setTimeout(() => r(null), ms));
    if (select === "first_bid") {
      const winnerOrNull = await Promise.race([firstBidArrived, sleep(deadlineMs)]);
      if (winnerOrNull === null && bids.length === 0) {
        await pathway.close();
        const err = new Error(`dispatchOffer: no BID arrived within ${deadlineMs}ms`);
        err.name = "TimeoutError";
        throw err;
      }
    } else {
      await sleep(deadlineMs);
      if (bids.length === 0) {
        await pathway.close();
        const err = new Error(`dispatchOffer: no BID arrived within ${deadlineMs}ms`);
        err.name = "TimeoutError";
        throw err;
      }
    }

    let winner: Signal;
    if (select === "first_bid") {
      winner = bids[0]!;
    } else if (select === "lowest_cost") {
      winner = bids.reduce((a, b) =>
        ((b.payload["cost"] as number | undefined) ?? Infinity) <
        ((a.payload["cost"] as number | undefined) ?? Infinity)
          ? b
          : a,
      );
    } else {
      winner = bids.reduce((a, b) =>
        ((b.payload["confidence"] as number | undefined) ?? -Infinity) >
        ((a.payload["confidence"] as number | undefined) ?? -Infinity)
          ? b
          : a,
      );
    }

    // Tell losers (informational).
    for (const b of bids) {
      if (b.id === winner.id) continue;
      const bNeuron = b.directed?.id ?? null;
      try {
        await this.emit(
          taskDeclinedSignal({
            traceId: tid,
            parentId: b.id,
            reason: "not selected",
            ...(bNeuron ? { directed: { id: bNeuron } } : {}),
          }),
        );
      } catch {
        /* best-effort */
      }
    }

    const winnerNeuron = winner.directed?.id ?? null;
    const winningBid: Json = {};
    for (const k of ["cost", "eta_ms", "confidence"]) {
      if (k in winner.payload) winningBid[k] = winner.payload[k];
    }
    const awarded = taskAwardedSignal({
      traceId: tid,
      parentId: winner.id,
      input: args.input,
      winningBid,
      finalize,
      ...(winnerNeuron ? { directed: { id: winnerNeuron } } : {}),
      ...(args.contextRef !== undefined ? { contextRef: args.contextRef } : {}),
    });
    try {
      await this.emit(awarded);
    } catch (err) {
      await pathway.close();
      throw err;
    }
    return pathway;
  }

  /**
   * Emit a BID in response to a TASK_OFFER, on behalf of the local Axon named
   * by `neuron`. Bypasses the role guard  -  a worker bidding announces
   * capability, not orchestration.
   */
  async bid(
    offer: Signal,
    args: { neuron: string; cost: number; etaMs?: number; confidence?: number; meta?: Json },
  ): Promise<Signal> {
    if (offer.type !== SignalType.TASK_OFFER) {
      throw new DendriteProtocolError(
        `bid() expects a TASK_OFFER signal, got '${offer.type}'`,
      );
    }
    const sig = bidSignal({
      traceId: offer.trace_id,
      parentId: offer.id,
      directed: { id: args.neuron },
      cost: args.cost,
      ...(args.etaMs !== undefined ? { etaMs: args.etaMs } : {}),
      ...(args.confidence !== undefined ? { confidence: args.confidence } : {}),
      ...(args.meta !== undefined ? { meta: args.meta } : {}),
    });
    await this.publish(sig);
    return sig;
  }

  /** Default bidder: first hosted Axon whose caps cover the offer answers
   *  (cost 0, confidence 1). No-op when nothing matches. */
  private async maybeAutoBid(offer: Signal): Promise<void> {
    const requested = new Set(
      (offer.payload["capabilities"] as string[] | undefined) ?? [],
    );
    for (const axon of this._axons.values()) {
      if (requested.size && ![...requested].every((c) => axon.capabilities.includes(c))) {
        continue;
      }
      try {
        await this.bid(offer, {
          neuron: axon.neuronId,
          cost: 0,
          confidence: 1,
          meta: { auto_bid: true },
        });
      } catch {
        /* best-effort */
      }
      return;
    }
  }

  // -- reply / cognition emit helpers ---------------------------------

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

  async emitPlan(args: {
    traceId: string;
    parentId: string;
    steps: unknown[];
    rationale?: string;
    neuron?: string;
    meta?: Json;
  }): Promise<Signal> {
    const sig = planSignal({
      traceId: args.traceId,
      parentId: args.parentId,
      steps: args.steps as Json[],
      directed: { id: args.neuron ?? this.dendriteId },
      ...(args.rationale !== undefined ? { rationale: args.rationale } : {}),
      ...(args.meta !== undefined ? { meta: args.meta } : {}),
    });
    await this.emit(sig);
    return sig;
  }

  async emitThoughtDelta(args: {
    traceId: string;
    parentId: string;
    delta: string;
    seq?: number;
    neuron?: string;
    meta?: Json;
  }): Promise<Signal> {
    const sig = thoughtDeltaSignal({
      traceId: args.traceId,
      parentId: args.parentId,
      delta: args.delta,
      directed: { id: args.neuron ?? this.dendriteId },
      ...(args.seq !== undefined ? { seq: args.seq } : {}),
      ...(args.meta !== undefined ? { meta: args.meta } : {}),
    });
    await this.emit(sig);
    return sig;
  }

  async emitToolCall(args: {
    traceId: string;
    parentId: string;
    tool: string;
    args_: Json;
    callId?: string;
    neuron?: string;
    meta?: Json;
  }): Promise<Signal> {
    const sig = toolCallSignal({
      traceId: args.traceId,
      parentId: args.parentId,
      tool: args.tool,
      args: args.args_,
      directed: { id: args.neuron ?? this.dendriteId },
      ...(args.callId !== undefined ? { callId: args.callId } : {}),
      ...(args.meta !== undefined ? { meta: args.meta } : {}),
    });
    await this.emit(sig);
    return sig;
  }

  async emitToolResult(args: {
    traceId: string;
    parentId: string;
    tool: string;
    result?: unknown;
    error?: string;
    callId?: string;
    neuron?: string;
    meta?: Json;
  }): Promise<Signal> {
    const sig = toolResultSignal({
      traceId: args.traceId,
      parentId: args.parentId,
      tool: args.tool,
      directed: { id: args.neuron ?? this.dendriteId },
      ...(args.result !== undefined ? { result: args.result } : {}),
      ...(args.error !== undefined ? { error: args.error } : {}),
      ...(args.callId !== undefined ? { callId: args.callId } : {}),
      ...(args.meta !== undefined ? { meta: args.meta } : {}),
    });
    await this.emit(sig);
    return sig;
  }

  async emitMemoryAppend(args: {
    traceId: string;
    parentId: string;
    key: string;
    value: unknown;
    neuron?: string;
    meta?: Json;
  }): Promise<Signal> {
    const sig = memoryAppendSignal({
      traceId: args.traceId,
      parentId: args.parentId,
      key: args.key,
      value: args.value,
      directed: { id: args.neuron ?? this.dendriteId },
      ...(args.meta !== undefined ? { meta: args.meta } : {}),
    });
    await this.emit(sig);
    return sig;
  }

  async emitCritique(args: {
    traceId: string;
    parentId: string;
    targetEventId: string;
    issues: Json[];
    verdict: "pass" | "fail" | "revise";
    neuron?: string;
    meta?: Json;
  }): Promise<Signal> {
    const sig = critiqueSignal({
      traceId: args.traceId,
      parentId: args.parentId,
      targetEventId: args.targetEventId,
      issues: args.issues,
      verdict: args.verdict,
      directed: { id: args.neuron ?? this.dendriteId },
      ...(args.meta !== undefined ? { meta: args.meta } : {}),
    });
    await this.emit(sig);
    return sig;
  }

  async emitEscalation(args: {
    traceId: string;
    parentId: string;
    reason: string;
    target?: string;
    context?: Json;
    neuron?: string;
    meta?: Json;
  }): Promise<Signal> {
    const sig = escalationSignal({
      traceId: args.traceId,
      parentId: args.parentId,
      reason: args.reason,
      directed: { id: args.neuron ?? this.dendriteId },
      ...(args.target !== undefined ? { target: args.target } : {}),
      ...(args.context !== undefined ? { context: args.context } : {}),
      ...(args.meta !== undefined ? { meta: args.meta } : {}),
    });
    await this.emit(sig);
    return sig;
  }

  async emitConsensus(args: {
    traceId: string;
    parentId: string;
    members: string[];
    verdict: string;
    votes?: Json;
    neuron?: string;
    meta?: Json;
  }): Promise<Signal> {
    const sig = consensusSignal({
      traceId: args.traceId,
      parentId: args.parentId,
      members: args.members,
      verdict: args.verdict,
      directed: { id: args.neuron ?? this.dendriteId },
      ...(args.votes !== undefined ? { votes: args.votes } : {}),
      ...(args.meta !== undefined ? { meta: args.meta } : {}),
    });
    await this.emit(sig);
    return sig;
  }

  async emitContextSync(args: {
    traceId: string;
    parentId: string;
    snapshot: Json;
    version?: string;
    neuron?: string;
    meta?: Json;
  }): Promise<Signal> {
    const sig = contextSyncSignal({
      traceId: args.traceId,
      parentId: args.parentId,
      snapshot: args.snapshot,
      directed: { id: args.neuron ?? this.dendriteId },
      ...(args.version !== undefined ? { version: args.version } : {}),
      ...(args.meta !== undefined ? { meta: args.meta } : {}),
    });
    await this.emit(sig);
    return sig;
  }

  // -- close-the-loop helpers ------------------------------------------

  /**
   * Reply to a CLARIFICATION by re-dispatching a TASK with the answer. The
   * new TASK is addressed by default to the asking Neuron, with parentId =
   * the clarification's id and the original traceId carried over. Input
   * shape: `{ clarification: { question, answer, ...extra } }`.
   */
  async respondToClarification(
    request: Signal,
    opts: { answer: unknown; extra?: Json; neuron?: string; meta?: Json },
  ): Promise<Signal> {
    if (request.type !== SignalType.CLARIFICATION) {
      throw new DendriteProtocolError(
        `respondToClarification expects a CLARIFICATION signal, got '${request.type}'`,
      );
    }
    const target = opts.neuron ?? request.directed?.id ?? null;
    if (!target) {
      throw new DendriteProtocolError(
        "respondToClarification: signal has no neuron and no neuron override - " +
          "nowhere to dispatch the follow-up TASK",
      );
    }
    const clarification: Json = {
      question: request.payload["question"] ?? null,
      answer: opts.answer,
    };
    if (opts.extra !== undefined) Object.assign(clarification, opts.extra);
    return this.dispatchTask({
      neuron: target,
      input: { clarification },
      traceId: request.trace_id,
      parentId: request.id,
      ...(opts.meta !== undefined ? { meta: opts.meta } : {}),
    });
  }

  /**
   * Reply to an ESCALATION by dispatching a TASK to the escalation target
   * (default: `payload.target`). Default input:
   * `{ escalation: { reason, context, from } }`.
   */
  async respondToEscalation(
    request: Signal,
    opts: { neuron?: string; input?: Json; meta?: Json } = {},
  ): Promise<Signal> {
    if (request.type !== SignalType.ESCALATION) {
      throw new DendriteProtocolError(
        `respondToEscalation expects an ESCALATION signal, got '${request.type}'`,
      );
    }
    const target = opts.neuron ?? (request.payload["target"] as string | undefined) ?? null;
    if (!target) {
      throw new DendriteProtocolError(
        "respondToEscalation: signal has no payload.target and no neuron " +
          "override - nowhere to dispatch the follow-up TASK",
      );
    }
    const input: Json =
      opts.input ?? {
        escalation: {
          reason: request.payload["reason"] ?? null,
          context: request.payload["context"] ?? null,
          from: request.directed?.id ?? null,
        },
      };
    return this.dispatchTask({
      neuron: target,
      input,
      traceId: request.trace_id,
      parentId: request.id,
      ...(opts.meta !== undefined ? { meta: opts.meta } : {}),
    });
  }

  /**
   * Reply to a PERMISSION by re-dispatching a TASK carrying the verdict.
   * Input shape: `{ permission: { action, granted, reason?, ttl_ms?, ...extra } }`.
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

  /** Approve a PERMISSION request. */
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

  /** Answer a CLARIFICATION with a discrete CLARIFICATION_ANSWER signal
   *  (parent_id = the request's id). Consumers pick it up via
   *  {@link onClarificationAnswer} or {@link awaitDecision}. Distinct from
   *  {@link respondToClarification}, which re-dispatches a TASK. */
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

  /** Emit a synapse-side Signal. Refuses Axon-owned types; TASK initiation
   *  additionally requires orchestrator role. */
  async emit(signal: Signal): Promise<void> {
    if (signal.type === SignalType.TASK) {
      this.requireOrchestrator(`emit(${signal.type})`);
    }
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

  /** Subject for capability-routed TASKs (queue-grouped consumers). */
  private routedSubject(): string {
    return `cosmonapse.${this.namespace}.${SignalType.TASK}.routed`;
  }

  private ensureInboundSub(type: SignalType): Promise<void> {
    if (this.inboundSubs.has(type)) return Promise.resolve();
    // Dedupe concurrent calls on the in-flight promise, not the completed
    // subscription  -  otherwise two racing callers each subscribe and every
    // handler fires twice.
    let p = this.inflightSubs.get(type);
    if (!p) {
      p = (async (): Promise<void> => {
        const sub = await this.subscribe(type, (s) => this.dispatchInbound(s));
        this.inboundSubs.set(type, sub);
      })().finally(() => this.inflightSubs.delete(type));
      this.inflightSubs.set(type, p);
    }
    return p;
  }

  /**
   * Route an inbound TASK to a local Axon. Addressed: by neuron_id (drop if
   * not hosted here). Capability-routed: first local Axon whose caps superset
   * the request. After publishing the reply, apply terminal-handler finalize:
   * a TASK tagged `payload.finalize` promotes a successful AGENT_OUTPUT by
   * also emitting FINAL (parented to the AGENT_OUTPUT, attributed to the
   * producing Neuron). Only AGENT_OUTPUT is promoted  -  CLARIFICATION /
   * PERMISSION pause the workflow and ERROR is already terminal.
   */
  private async onTask(task: Signal): Promise<void> {
    let target = task.directed?.id ?? null;
    let axon: Axon | undefined;

    if (target) {
      axon = this._axons.get(target);
      if (!axon) return;
    } else {
      const requested = (task.payload["capabilities"] as string[] | undefined) ?? [];
      if (!requested.length) return;
      for (const candidate of this._axons.values()) {
        if (requested.every((c) => candidate.capabilities.includes(c))) {
          axon = candidate;
          break;
        }
      }
      if (!axon) return;
      target = axon.neuronId;
    }

    const ac = new AbortController();
    this.registerTraceAbort(task.trace_id, ac);
    let reply: Signal | null;
    try {
      reply = await this.raceAbort(axon.handleTask(task), ac.signal);
    } catch (err) {
      reply = errorSignal({
        traceId: task.trace_id,
        parentId: task.id,
        directed: { id: target },
        code: "AXON_EXCEPTION",
        message: err instanceof Error ? err.message : String(err),
        recoverable: false,
      });
    } finally {
      this.unregisterTraceAbort(task.trace_id, ac);
    }
    if (reply === null) {
      // STOP abandoned this trace; suppress the reply (STOPPED ack covers it).
      return;
    }
    await this.publish(reply);

    if (reply.type === SignalType.AGENT_OUTPUT && task.payload["finalize"]) {
      try {
        await this.publish(
          finalSignal({
            traceId: reply.trace_id,
            parentId: reply.id,
            directed: { id: target },
            result: (reply.payload["output"] as Json | undefined) ?? {},
          }),
        );
      } catch {
        /* best-effort: promotion failure must not break the reply path */
      }
    }
  }

  // -- workflow control: STOP / STOPPED -------------------------------

  private registerTraceAbort(traceId: string, ac: AbortController): void {
    let set = this.traceAborts.get(traceId);
    if (!set) {
      set = new Set();
      this.traceAborts.set(traceId, set);
    }
    set.add(ac);
  }

  private unregisterTraceAbort(traceId: string, ac: AbortController): void {
    const set = this.traceAborts.get(traceId);
    if (set) {
      set.delete(ac);
      if (set.size === 0) this.traceAborts.delete(traceId);
    }
  }

  /** Resolve to the promise's value, or to null if the signal aborts first. */
  private raceAbort<T>(p: Promise<T>, signal: AbortSignal): Promise<T | null> {
    if (signal.aborted) return Promise.resolve(null);
    return new Promise<T | null>((resolve, reject) => {
      const onAbort = (): void => resolve(null);
      signal.addEventListener("abort", onAbort, { once: true });
      p.then(
        (v) => {
          signal.removeEventListener("abort", onAbort);
          resolve(v);
        },
        (e) => {
          signal.removeEventListener("abort", onAbort);
          reject(e);
        },
      );
    });
  }

  private async onStop(signal: Signal): Promise<void> {
    const traceId = signal.trace_id;
    if (!traceId) return;
    const rollback = Boolean((signal.payload as Record<string, unknown>)["rollback"]);
    let cancelled = 0;
    let compensated = 0;
    let didWork = false;

    const acs = this.traceAborts.get(traceId);
    if (acs) {
      for (const ac of acs) {
        if (!ac.signal.aborted) {
          ac.abort();
          cancelled++;
        }
      }
      this.traceAborts.delete(traceId);
      didWork = true;
    }

    try {
      await this.cancelOpPathways(traceId);
      this.engramClient.cancelTrace(traceId);
    } catch {
      /* best-effort */
    }

    for (const engram of this._engrams.values()) {
      try {
        if (rollback) {
          const n = await engram.compensate(traceId);
          if (n > 0) {
            compensated += n;
            didWork = true;
          }
        } else {
          await engram.commit(traceId);
        }
      } catch {
        /* best-effort */
      }
    }

    const pw = this.pathways.get(traceId);
    if (pw && !pw.closed) {
      didWork = true;
      try {
        await pw.close();
      } catch {
        /* best-effort */
      }
    }

    if (didWork) {
      try {
        await this.publish(
          stoppedSignal({
            traceId,
            parentId: signal.id,
            node: this.namespace,
            rolledBack: rollback,
            cancelled,
            compensated,
          }),
        );
      } catch {
        /* best-effort ack */
      }
    }
  }

  /** Broadcast a STOP for `traceId` (orchestrator-gated). Best-effort and
   *  idempotent. */
  async emitStop(args: { traceId: string; rollback?: boolean; reason?: string }): Promise<Signal> {
    this.requireOrchestrator("emitStop");
    await this.ensureInboundSub(SignalType.STOP);
    const sig = stopSignal({
      traceId: args.traceId,
      ...(args.rollback !== undefined ? { rollback: args.rollback } : {}),
      ...(args.reason !== undefined ? { reason: args.reason } : {}),
    });
    await this.publish(sig);
    return sig;
  }

  /** Stop a whole workflow. With `collectAcks` returns the STOPPED acks seen
   *  within `timeoutMs` (best effort). */
  async stopTrace(
    traceId: string,
    opts: { rollback?: boolean; reason?: string; collectAcks?: boolean; timeoutMs?: number } = {},
  ): Promise<Signal[]> {
    if (!opts.collectAcks) {
      await this.emitStop({
        traceId,
        ...(opts.rollback !== undefined ? { rollback: opts.rollback } : {}),
        ...(opts.reason !== undefined ? { reason: opts.reason } : {}),
      });
      return [];
    }
    const acks: Signal[] = [];
    const collect = async (sig: Signal): Promise<void> => {
      if (sig.trace_id === traceId) acks.push(sig);
    };
    const list = this.handlers.get(SignalType.STOPPED) ?? [];
    list.push(collect);
    this.handlers.set(SignalType.STOPPED, list);
    await this.ensureInboundSub(SignalType.STOPPED);
    try {
      await this.emitStop({
        traceId,
        ...(opts.rollback !== undefined ? { rollback: opts.rollback } : {}),
        ...(opts.reason !== undefined ? { reason: opts.reason } : {}),
      });
      await new Promise((r) => setTimeout(r, opts.timeoutMs ?? 1000));
    } finally {
      const idx = (this.handlers.get(SignalType.STOPPED) ?? []).indexOf(collect);
      if (idx >= 0) (this.handlers.get(SignalType.STOPPED) ?? []).splice(idx, 1);
    }
    return acks;
  }

  // -- retry ----------------------------------------------------------

  private async safeStop(traceId: string, retry: RetryStrategy): Promise<void> {
    try {
      await this.emitStop({
        traceId,
        rollback: Boolean(retry.rollbackOnRetry),
        reason: retry.reason ?? "retry",
      });
    } catch {
      /* best-effort preemptive STOP */
    }
  }

  /** Dispatch and wait, retrying per `retry` until a non-retryable outcome or
   *  attempts are exhausted. Returns the resolved Signal; re-throws the last
   *  error when every attempt failed with an exception. */
  async runWithRetry(
    args: DispatchArgs & { scope?: PathwayScope; finalize?: boolean; timeoutMs?: number; retry: RetryStrategy },
  ): Promise<Signal> {
    const { retry, timeoutMs, traceId: callerTrace, ...rest } = args;
    const maxAttempts = retry.maxAttempts ?? 3;
    const retryOn = retry.retryOn ?? defaultRetryOn;
    const newTrace = retry.newTrace ?? true;
    const perTimeout = retry.timeoutMs ?? timeoutMs ?? 30_000;

    let outcome: RetryOutcome | null = null;
    for (let attempt = 0; attempt < maxAttempts; attempt++) {
      const tid = callerTrace && !newTrace ? callerTrace : newTraceId();
      const meta = { ...(rest.meta ?? {}), attempt };
      try {
        const pathway = await this.dispatch({ ...rest, traceId: tid, meta });
        try {
          outcome = await pathway.wait(perTimeout);
        } finally {
          await pathway.close();
        }
      } catch (err) {
        outcome = err instanceof Error ? err : new Error(String(err));
      }

      if (!retryOn(outcome)) {
        if (outcome instanceof Error) throw outcome;
        return outcome;
      }
      if (newTrace) await this.safeStop(tid, retry);
      if (attempt + 1 >= maxAttempts) {
        if (outcome instanceof Error) throw outcome;
        return outcome;
      }
      if (retry.onRetry) {
        try {
          retry.onRetry(attempt, outcome);
        } catch {
          /* hook errors are non-fatal */
        }
      }
      const delay = retry.backoffMs ? retry.backoffMs(attempt) : 0;
      if (delay > 0) await new Promise((r) => setTimeout(r, delay));
    }
    throw new Error("runWithRetry: exhausted attempts unexpectedly");
  }

  private async emitRegister(axon: Axon): Promise<void> {
    await this.publish(
      registerSignal({
        directed: {
          id: axon.neuronId,
          type: axon.neuronKind ?? "neuron",
          capabilities: [...axon.capabilities],
        },
        capabilities: axon.capabilities,
        role: "neuron",
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
    if (this.registryStore !== null && this.staleAfterMs > 0) {
      try {
        await this.sweepStaleNeurons(Date.now());
      } catch {
        /* best-effort sweep */
      }
    }
  }

  /** Mark Neurons deregistered when their last heartbeat is older than
   *  `staleAfterMs`. Own hosted Axons were touched immediately before the
   *  sweep, so they never qualify. */
  private async sweepStaleNeurons(nowMs: number): Promise<void> {
    const store = this.registryStore;
    if (store === null) return;
    const records = await store.list({ includeDeregistered: false });
    for (const rec of records) {
      const seen = rec.last_heartbeat ?? rec.registered_at;
      if (!seen) continue;
      if (nowMs - Date.parse(seen) > this.staleAfterMs) {
        try {
          await store.markDeregistered(rec.neuron_id);
          await this.hooks._fireRefresh({
            reason: "stale",
            neuronId: rec.neuron_id,
            extra: {},
          });
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

  /** Respond to a DISCOVER by re-emitting REGISTER for matching Axons. */
  private async respondToDiscover(signal: Signal): Promise<void> {
    if (this._axons.size === 0) return;
    const target = signal.payload["neuron"] as string | undefined;
    const capsFilter = signal.payload["capabilities"] as string[] | undefined;
    for (const axon of this._axons.values()) {
      if (target && axon.neuronId !== target) continue;
      if (capsFilter?.length && !capsFilter.every((c) => axon.capabilities.includes(c))) {
        continue;
      }
      try {
        await this.emitRegister(axon);
      } catch {
        /* best-effort */
      }
    }
  }

  private async dispatchInbound(signal: Signal): Promise<void> {
    if (signal.type === SignalType.DISCOVER) {
      const hs = this.handlers.get(SignalType.DISCOVER) ?? [];
      if (hs.length) await Promise.allSettled(hs.map((h) => h(signal)));
      else await this.respondToDiscover(signal);
      return;
    }

    // Engram I/O requests: route RECALL/IMPRINT to hosted Engrams (server
    // side), then fire observer handlers and return.
    if (signal.type === SignalType.RECALL) {
      await this.onRecall(signal);
      const hs = this.handlers.get(SignalType.RECALL) ?? [];
      if (hs.length) await Promise.allSettled(hs.map((h) => h(signal)));
      return;
    }
    if (signal.type === SignalType.IMPRINT) {
      await this.onImprint(signal);
      const hs = this.handlers.get(SignalType.IMPRINT) ?? [];
      if (hs.length) await Promise.allSettled(hs.map((h) => h(signal)));
      return;
    }

    // Workflow control: a STOP cancels everything this Dendrite owns on the
    // trace. Deliver to trace observers first (so on(STOP) fires), then quiesce.
    if (signal.type === SignalType.STOP) {
      if (signal.trace_id) {
        const pw = this.pathways.get(signal.trace_id);
        if (pw) {
          try {
            await pw._deliver(signal);
          } catch {
            /* deliver must not break the inbound path */
          }
        }
      }
      await this.onStop(signal);
      const hs = this.handlers.get(SignalType.STOP) ?? [];
      if (hs.length) await Promise.allSettled(hs.map((h) => h(signal)));
      return;
    }

    // Engram I/O responses: resolve the caller-side correlation table.
    // Delivery continues below so pathways/observers still see them.
    if (signal.type === SignalType.RECALLED || signal.type === SignalType.IMPRINTED) {
      this.engramClient.deliver(signal);
    }

    // Engram registration: a REGISTER carrying payload.role === "engram" (or
    // the legacy flag, or a directed.type matching a known engram kind)
    // announces an Engram participant, not a Neuron. Record it and stop  -
    // it must not pollute the Neuron registry or fire onRegister handlers.
    if (signal.type === SignalType.REGISTER && this.isEngramRegister(signal)) {
      this.recordEngramRegistration(signal);
      return;
    }

    // Per-operation (request/reply) correlation: deliver any Signal whose
    // parent_id matches an open op-Pathway (awaitDecision). Op ids are unique
    // envelope ids, so this never misroutes; delivery continues below so
    // trace observers still see the Signal.
    if (signal.parent_id) {
      const opPw = this.opPathways.get(signal.parent_id);
      if (opPw) {
        try {
          await opPw._deliver(signal);
        } catch {
          /* op delivery must not break the inbound path */
        }
      }
    }

    // Cache discrete decisions by parent_id so a later awaitDecision can
    // still resolve when the answer beat it onto the bus.
    if (
      (signal.type === SignalType.CLARIFICATION_ANSWER ||
        signal.type === SignalType.PERMISSION_DECISION) &&
      signal.parent_id
    ) {
      this.recentDecisions.set(signal.parent_id, signal);
      while (this.recentDecisions.size > 256) {
        const oldest = this.recentDecisions.keys().next().value as string;
        this.recentDecisions.delete(oldest);
      }
    }

    // Trace terminal events cancel in-flight op I/O on the same trace  -
    // both the generic op-pathways and the EngramClient's pending table.
    if (
      (signal.type === SignalType.FINAL || signal.type === SignalType.ERROR) &&
      signal.trace_id
    ) {
      await this.cancelOpPathways(signal.trace_id);
      this.engramClient.cancelTrace(signal.trace_id);
      // Saga commit point is success (FINAL) only: discard each hosted
      // Engram's journal so successful writes become permanent. On ERROR the
      // journal is kept so the caller can still stopTrace({rollback:true}) to
      // compensate a failed workflow (a plain stop, or a successful retry's
      // preemptive STOP, discards it).
      if (signal.type === SignalType.FINAL) {
        for (const engram of this._engrams.values()) {
          try {
            await engram.commit(signal.trace_id);
          } catch {
            /* best-effort commit */
          }
        }
      }
      this.traceAborts.delete(signal.trace_id);
    }

    // TASK_AWARDED targeting one of our Axons: synthesise a TASK (carrying
    // the finalize tag) and route through the existing Axon handler.
    if (signal.type === SignalType.TASK_AWARDED) {
      const target = signal.directed?.id ?? null;
      if (target && this._axons.has(target)) {
        const synthetic = taskSignal({
          traceId: signal.trace_id,
          parentId: signal.id,
          directed: { id: target },
          input: (signal.payload["input"] as Json | undefined) ?? {},
          finalize: Boolean(signal.payload["finalize"]),
          ...(signal.payload["context_ref"] !== undefined
            ? { contextRef: signal.payload["context_ref"] as string }
            : {}),
          meta: signal.meta,
        });
        await this.onTask(synthetic);
      }
    }

    // Trace-matched Pathway delivery.
    if (signal.trace_id && PATHWAY_TYPES.has(signal.type)) {
      const pathway = this.pathways.get(signal.trace_id);
      if (pathway) {
        try {
          await pathway._deliver(signal);
        } catch {
          /* pathway delivery must not break the inbound path */
        }
      }
    }

    if (AXON_TYPES.has(signal.type) && this.registryStore !== null) {
      try {
        await this.updateRegistry(signal);
      } catch {
        /* best-effort registry update */
      }
    }

    // Default bidder: unless the developer registered their own onTaskOffer
    // handler (custom bidding wins outright) or autoBid is off.
    if (
      signal.type === SignalType.TASK_OFFER &&
      this.autoBid &&
      this._axons.size > 0 &&
      (this.handlers.get(SignalType.TASK_OFFER) ?? []).length === 0
    ) {
      await this.maybeAutoBid(signal);
    }

    const handlers = this.handlers.get(signal.type) ?? [];
    if (handlers.length) await Promise.allSettled(handlers.map((h) => h(signal)));
  }

  // -- Engram: hosted-side handlers -----------------------------------

  /** Pick the hosted Engrams that should respond to a RECALL/IMPRINT.
   *  directed.id (engramId) wins over directed.type (engramKind). */
  private resolveEngramTargets(signal: Signal): Engram[] {
    const eid = signal.directed?.id ?? null;
    if (eid) {
      const ent = this._engrams.get(eid);
      return ent ? [ent] : [];
    }
    const ekind = signal.directed?.type ?? null;
    if (ekind) {
      return (this.engramKindIndex.get(ekind) ?? [])
        .map((id) => this._engrams.get(id))
        .filter((e): e is Engram => e !== undefined);
    }
    return [];
  }

  private async onRecall(signal: Signal): Promise<void> {
    const targets = this.resolveEngramTargets(signal);
    if (!targets.length) return;
    const query = (signal.payload["query"] as Json | undefined) ?? {};
    const filters = signal.payload["filters"] as Json | undefined;
    const contextRef = signal.payload["context_ref"] as string | undefined;
    const deadlineMs = signal.payload["deadline_ms"] as number | undefined;
    const minConfidence = signal.payload["min_confidence"] as number | undefined;
    for (const engram of targets) {
      let hits;
      try {
        if (!(await engram.canServe(query))) continue;
        hits = await engram.recall(query, {
          ...(filters !== undefined ? { filters } : {}),
          ...(contextRef !== undefined ? { contextRef } : {}),
          ...(deadlineMs !== undefined ? { deadlineMs } : {}),
          ...(minConfidence !== undefined ? { minConfidence } : {}),
        });
      } catch {
        continue; // a failing backend must not break the host
      }
      try {
        await this.publish(
          recalledSignal({
            traceId: signal.trace_id,
            parentId: signal.id,
            engramId: engram.engramId,
            hits: hits.map((h) => ({ id: h.id, entry: h.entry, score: h.score })),
            // Attribute the reply to the Engram that answered, not the host
            // Dendrite, so observers classify it by the Engram's REGISTER.
            directed: { id: engram.engramId, type: engram.engramKind },
          }),
        );
      } catch {
        /* best-effort reply */
      }
    }
  }

  private async onImprint(signal: Signal): Promise<void> {
    const targets = this.resolveEngramTargets(signal);
    if (!targets.length) return;
    const op = (signal.payload["op"] as ImprintOp | undefined) ?? ("add" as ImprintOp);
    const entry = (signal.payload["entry"] as Json | undefined) ?? {};
    const mergeKey = signal.payload["merge_key"] as string | undefined;
    for (const engram of targets) {
      let reply: Signal;
      try {
        const receipt: ImprintReceipt = await engram.imprint(op, entry, {
          imprintId: signal.id,
          traceId: signal.trace_id,
          ...(mergeKey !== undefined ? { mergeKey } : {}),
        });
        reply = imprintedSignal({
          traceId: signal.trace_id,
          parentId: signal.id,
          engramId: receipt.engramId || engram.engramId,
          op: receipt.op,
          ...(receipt.id !== null ? { id: receipt.id } : {}),
          ...(receipt.version !== null ? { version: receipt.version } : {}),
          ...(receipt.tookMs !== null ? { tookMs: receipt.tookMs } : {}),
          ...(receipt.error !== null ? { error: receipt.error } : {}),
          directed: { id: engram.engramId, type: engram.engramKind },
        });
      } catch (err) {
        reply = imprintedSignal({
          traceId: signal.trace_id,
          parentId: signal.id,
          engramId: engram.engramId,
          op,
          error: `engram_exception: ${err instanceof Error ? err.message : String(err)}`,
          directed: { id: engram.engramId, type: engram.engramKind },
        });
      }
      try {
        await this.publish(reply);
      } catch {
        /* best-effort reply */
      }
    }
  }

  // -- Engram: registration (announce + learn) -------------------------

  private async emitEngramRegister(engram: Engram): Promise<void> {
    await this.publish(
      registerSignal({
        directed: {
          id: engram.engramId,
          type: engram.engramKind,
          capabilities: [...engram.capabilities],
        },
        capabilities: engram.capabilities,
        role: "engram",
        ...(engram.version !== null ? { version: engram.version } : {}),
      }),
    );
  }

  private isEngramRegister(signal: Signal): boolean {
    if (signal.payload["role"] === "engram" || signal.payload["engram"]) return true;
    const dtype = signal.directed?.type ?? null;
    if (dtype && (this.engramKindIndex.has(dtype) || this.engramRegKindIndex.has(dtype))) {
      return true;
    }
    return false;
  }

  private recordEngramRegistration(signal: Signal): void {
    const d = signal.directed;
    if (!d || (!d.id && !d.type)) return;
    let caps = [...d.capabilities];
    if (!caps.length) {
      caps = [...((signal.payload["capabilities"] as string[] | undefined) ?? [])];
    }
    const key = d.id ?? d.type!;
    this._engramRegistrations.set(key, { id: d.id, type: d.type, capabilities: caps });
    if (d.type) {
      const bucket = this.engramRegKindIndex.get(d.type) ?? new Set<string>();
      bucket.add(key);
      this.engramRegKindIndex.set(d.type, bucket);
    }
  }

  // -- Engram: caller-side helpers -------------------------------------

  /** Resolve (traceId, parentId) for a caller-side engram op: explicit ids
   *  win, then the ambient task context (bound by Axon.handleTask), then a
   *  freshly minted trace (the pre-task-hydration shape). */
  private static resolveTrace(
    traceId: string | undefined,
    parentId: string | undefined,
  ): [string, string] {
    let tid = traceId;
    let pid = parentId;
    if (tid === undefined) {
      const amb = ambientTrace();
      if (amb !== null) {
        tid = amb[0];
        if (pid === undefined) pid = amb[1];
      }
    }
    return [tid ?? newTraceId(), pid ?? newEventId()];
  }

  /** Emit RECALL and await RECALLED. Trace attribution: explicit ids win,
   *  then the ambient task context, then a fresh trace. */
  async recall(args: {
    engramId?: string;
    engramKind?: string;
    query: Json;
    filters?: Json;
    contextRef?: string;
    deadlineMs?: number;
    recallMode?: RecallMode;
    minConfidence?: number;
    traceId?: string;
    parentId?: string;
    meta?: Json;
  }): Promise<RecallResult> {
    const [tid, pid] = Dendrite.resolveTrace(args.traceId, args.parentId);
    return this.engramClient.recall({
      query: args.query,
      traceId: tid,
      parentId: pid,
      ...(args.engramId !== undefined ? { engramId: args.engramId } : {}),
      ...(args.engramKind !== undefined ? { engramKind: args.engramKind } : {}),
      ...(args.filters !== undefined ? { filters: args.filters } : {}),
      ...(args.contextRef !== undefined ? { contextRef: args.contextRef } : {}),
      ...(args.deadlineMs !== undefined ? { deadlineMs: args.deadlineMs } : {}),
      ...(args.recallMode !== undefined ? { recallMode: args.recallMode } : {}),
      ...(args.minConfidence !== undefined ? { minConfidence: args.minConfidence } : {}),
      ...(args.meta !== undefined ? { meta: args.meta } : {}),
    });
  }

  /** Emit IMPRINT. Resolves null unless `awaitAck: true`. Trace attribution
   *  as {@link recall}. */
  async imprint(args: {
    engramId?: string;
    engramKind?: string;
    op: ImprintOp;
    entry: Json;
    mergeKey?: string;
    awaitAck?: boolean;
    deadlineMs?: number;
    traceId?: string;
    parentId?: string;
    meta?: Json;
  }): Promise<ImprintReceipt | null> {
    const [tid, pid] = Dendrite.resolveTrace(args.traceId, args.parentId);
    return this.engramClient.imprint({
      op: args.op,
      entry: args.entry,
      traceId: tid,
      parentId: pid,
      ...(args.engramId !== undefined ? { engramId: args.engramId } : {}),
      ...(args.engramKind !== undefined ? { engramKind: args.engramKind } : {}),
      ...(args.mergeKey !== undefined ? { mergeKey: args.mergeKey } : {}),
      ...(args.awaitAck !== undefined ? { awaitAck: args.awaitAck } : {}),
      ...(args.deadlineMs !== undefined ? { deadlineMs: args.deadlineMs } : {}),
      ...(args.meta !== undefined ? { meta: args.meta } : {}),
    });
  }

  private async updateRegistry(signal: Signal): Promise<void> {
    if (this.registryStore === null) return;
    // Engram registrations are not Neurons; don't mirror them.
    if (signal.payload["role"] === "engram" || signal.payload["engram"]) return;
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
