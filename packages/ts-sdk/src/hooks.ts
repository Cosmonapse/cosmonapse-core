/**
 * @cosmonapse/sdk — lifecycle hooks
 *
 * Shared lifecycle-hook surface for Axon and Dendrite / Cortex, ported from
 * `cosmonapse._hooks`. Three hook kinds cover both the centralised
 * (orchestrator-first) and decentralised (peer-to-peer) cases:
 *
 *   onConnect   fire-once after the component finishes its own connect
 *               handshake (Axon attached + registered, Dendrite up on the
 *               Synapse).
 *   onRefresh   fired whenever the component's observable state refreshes —
 *               heartbeat tick, REGISTER / DEREGISTER / HEARTBEAT seen by the
 *               registry, or a manual `refresh()`. The handler receives a
 *               {@link RefreshEvent} describing what changed.
 *   onSchedule  developer-supplied periodic task. Runs as a background loop
 *               every `everyMs` until the component stops.
 *
 * Decentralised use case — each Dendrite can announce itself on connect,
 * reconcile peers on refresh, and gossip on a schedule, so peer-to-peer
 * fabrics emerge without the SDK baking in an orchestration model.
 *
 * Python uses a mixin; TypeScript favours composition, so the host holds a
 * `LifecycleHooks` instance and drives it (`_fireConnect`, `_launchSchedule`,
 * `_fireRefresh`, `_stopHooks`). The `on*` decorators are re-exposed on the
 * host (Axon / Dendrite) for ergonomics.
 */

/** Context passed to onRefresh hooks. */
export interface RefreshEvent {
  /** "heartbeat" | "register" | "deregister" | "manual" | "scheduled" */
  reason: string;
  /** The neuron implicated in the change, if any. */
  neuronId?: string | null;
  /** Free-form bag for component-specific detail. */
  extra: Record<string, unknown>;
}

export type ConnectHook<O> = (owner: O) => void | Promise<void>;
export type RefreshHook<O> = (owner: O, event: RefreshEvent) => void | Promise<void>;
export type ScheduleHook<O> = (owner: O) => void | Promise<void>;

/**
 * Lifecycle-hook registry + driver. Generic over the owner type so handlers
 * receive a fully-typed back-reference to the component they're attached to.
 */
export class LifecycleHooks<O> {
  private readonly connectHooks: ConnectHook<O>[] = [];
  private readonly refreshHooks: RefreshHook<O>[] = [];
  private readonly scheduleHooks: Array<[number, ScheduleHook<O>]> = [];
  private readonly timers = new Set<ReturnType<typeof setTimeout>>();
  private started = false;

  constructor(private readonly owner: O) {}

  // -- decorators / registration ------------------------------------

  /** Register a fire-once handler called after the host finishes start(). */
  onConnect(fn: ConnectHook<O>): ConnectHook<O> {
    this.connectHooks.push(fn);
    return fn;
  }

  /** Register a handler called whenever the host's state refreshes. */
  onRefresh(fn: RefreshHook<O>): RefreshHook<O> {
    this.refreshHooks.push(fn);
    return fn;
  }

  /**
   * Register a periodic handler. The background loop runs every `everyMs`
   * for the lifetime of the host. If the host is already running, the loop
   * starts immediately.
   */
  onSchedule(everyMs: number, fn: ScheduleHook<O>): ScheduleHook<O> {
    if (everyMs <= 0) throw new Error("onSchedule requires everyMs > 0");
    this.scheduleHooks.push([everyMs, fn]);
    if (this.started) this.spawnLoop(everyMs, fn);
    return fn;
  }

  // -- driven by the host component ---------------------------------

  /** @internal */
  async _fireConnect(): Promise<void> {
    for (const h of [...this.connectHooks]) {
      try {
        await h(this.owner);
      } catch {
        /* a connect hook must never break startup */
      }
    }
  }

  /** @internal */
  async _fireRefresh(event: RefreshEvent): Promise<void> {
    for (const h of [...this.refreshHooks]) {
      try {
        await h(this.owner, event);
      } catch {
        /* a refresh hook must never break the heartbeat / registry path */
      }
    }
  }

  /** @internal */
  _launchSchedule(): void {
    if (this.started) return;
    for (const [interval, fn] of this.scheduleHooks) this.spawnLoop(interval, fn);
    this.started = true;
  }

  /** @internal */
  _stopHooks(): void {
    for (const t of this.timers) clearTimeout(t);
    this.timers.clear();
    this.started = false;
  }

  /**
   * Manually fire a refresh event. Useful when a developer's own code knows
   * internal state has changed.
   */
  async refresh(
    opts: { reason?: string; neuronId?: string | null; extra?: Record<string, unknown> } = {},
  ): Promise<void> {
    await this._fireRefresh({
      reason: opts.reason ?? "manual",
      neuronId: opts.neuronId ?? null,
      extra: opts.extra ?? {},
    });
  }

  // -- internal -----------------------------------------------------

  private spawnLoop(interval: number, fn: ScheduleHook<O>): void {
    const schedule = (): void => {
      const timer = setTimeout(() => {
        this.timers.delete(timer);
        void tick();
      }, interval);
      (timer as { unref?: () => void }).unref?.();
      this.timers.add(timer);
    };
    const tick = async (): Promise<void> => {
      try {
        await fn(this.owner);
      } catch {
        /* a scheduled hook throwing must not kill the loop */
      }
      if (this.started) schedule();
    };
    schedule();
  }
}
