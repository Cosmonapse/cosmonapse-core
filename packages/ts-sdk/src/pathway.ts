/**
 * @cosmonapse/sdk  -  pathway
 *
 * The Pathway primitive  -  a per-trace event handle, ported from
 * `cosmonapse.pathway`.
 *
 * A Pathway is the client-side observation surface for one logical workflow,
 * identified by its `trace_id`. Open one with
 * `dendrite.dispatch({ neuron, input })` (you become the *originator*), or
 * `dendrite.observePathway(traceId)` to watch a trace another peer started
 * (*observer*). Every Signal whose `trace_id` matches is delivered into it.
 *
 * Three consumption shapes on the same primitive:
 *
 * - `await pathway.wait()`            -  resolve on the next AGENT_OUTPUT /
 *   CLARIFICATION / PERMISSION / ERROR / FINAL (request/reply shape).
 * - `pathway.on(SignalType.X, fn)`    -  callback per matching Signal
 *   (reactive shape).
 * - `for await (const sig of pathway)` -  iterate every Signal until close
 *   (streaming shape).
 *
 * The shapes compose: callbacks, iteration and `wait()` each observe every
 * Signal independently  -  broadcasting, not draining a queue.
 *
 * Lifecycle: auto-closes on the first FINAL or ERROR; close explicitly with
 * `await pathway.close()`; the owning Dendrite closes survivors on `stop()`.
 */

import { SignalType, type Signal } from "./envelope.js";

/** Signals that auto-close a Pathway. AGENT_OUTPUT alone does NOT close it
 *  because a streaming workflow may produce several before finalising. */
export const TERMINAL_TYPES: ReadonlySet<SignalType> = new Set<SignalType>([
  SignalType.FINAL,
  SignalType.ERROR,
]);

/** Default set satisfying a bare `wait()`. CLARIFICATION / PERMISSION are
 *  included because both *pause* the workflow awaiting a human/peer decision,
 *  so a waiting orchestrator must surface them rather than hang. Callers that
 *  may receive these must inspect `.type`. */
const WAIT_TYPES: ReadonlySet<SignalType> = new Set<SignalType>([
  SignalType.AGENT_OUTPUT,
  SignalType.CLARIFICATION,
  SignalType.PERMISSION,
  SignalType.ERROR,
  SignalType.FINAL,
]);

/** Delivered when scope="terminal": the decentralised pattern  -  the Cortex
 *  only wakes for a conclusion, or a decision the workflow is blocked on. */
const SCOPE_TERMINAL_TYPES: ReadonlySet<SignalType> = new Set<SignalType>([
  SignalType.FINAL,
  SignalType.ERROR,
  SignalType.CLARIFICATION,
  SignalType.PERMISSION,
]);

/**
 * Signal types that flow through a Pathway. Excludes management types
 * (REGISTER / DEREGISTER / HEARTBEAT / DISCOVER  -  own trace_id space) and
 * TASK (the originator knows it dispatched; excluding TASK also avoids a
 * double subscription on Dendrites that both host Axons and dispatch).
 */
export const PATHWAY_TYPES: ReadonlySet<SignalType> = new Set<SignalType>(
  (Object.values(SignalType) as SignalType[]).filter(
    (t) =>
      t !== SignalType.TASK &&
      t !== SignalType.REGISTER &&
      t !== SignalType.DEREGISTER &&
      t !== SignalType.HEARTBEAT &&
      t !== SignalType.DISCOVER,
  ),
);

export type PathwaySignalHandler = (signal: Signal) => void | Promise<void>;
export type PathwayCloseHook = (pathway: Pathway) => void | Promise<void>;

/** Raised when `wait()` is called on (or interrupted by) a closed Pathway. */
export class PathwayClosedError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "PathwayClosedError";
  }
}

export type PathwayRole = "originator" | "observer";
export type PathwayScope = "all" | "terminal";

export interface PathwayOptions {
  traceId: string;
  /** Per-operation correlation key: when set, the owning Dendrite routes
   *  inbound Signals here by `signal.parent_id === parentId` (request/reply)
   *  instead of by trace. This is what lets request/reply clients (e.g. an
   *  EngramClient, `awaitDecision`) be thin wrappers over a Pathway. */
  parentId?: string | null;
  role?: PathwayRole;
  onClose?: PathwayCloseHook;
  /** "all" (default): every PATHWAY_TYPES Signal on the trace. "terminal":
   *  only FINAL / ERROR / CLARIFICATION / PERMISSION  -  registered `on()`
   *  callbacks still fire for scoped-out types (explicit interest). */
  scope?: PathwayScope;
}

interface Waiter {
  types: ReadonlySet<SignalType>;
  resolve: (s: Signal) => void;
  reject: (e: Error) => void;
  settled: boolean;
}

export class Pathway implements AsyncIterable<Signal> {
  readonly traceId: string;
  readonly parentId: string | null;
  readonly role: PathwayRole;
  readonly scope: PathwayScope;

  private readonly scopeFilter: ReadonlySet<SignalType> | null;
  private readonly onCloseHook: PathwayCloseHook | undefined;
  private readonly handlers = new Map<SignalType, PathwaySignalHandler[]>();
  private waiters: Waiter[] = [];
  private buffered: Signal[] = [];
  private closed_ = false;

  // Async iteration: a pull queue of pending `next()` resolvers and a push
  // queue of undelivered values. `null` is the close sentinel.
  private iterPush: Array<Signal | null> = [];
  private iterPull: Array<(v: IteratorResult<Signal>) => void> = [];

  constructor(opts: PathwayOptions) {
    const scope = opts.scope ?? "all";
    if (scope !== "all" && scope !== "terminal") {
      throw new Error(`scope must be 'all' or 'terminal', got '${scope as string}'`);
    }
    this.traceId = opts.traceId;
    this.parentId = opts.parentId ?? null;
    this.role = opts.role ?? "originator";
    this.scope = scope;
    this.scopeFilter = scope === "terminal" ? SCOPE_TERMINAL_TYPES : null;
    this.onCloseHook = opts.onClose;
  }

  get closed(): boolean {
    return this.closed_;
  }

  // -- consumer shape #1: wait ---------------------------------------

  /** Resolve on the next AGENT_OUTPUT, CLARIFICATION, PERMISSION, ERROR or
   *  FINAL. Rejects with PathwayClosedError if the Pathway closes first, and
   *  with a TimeoutError-named Error if `timeoutMs` elapses. */
  async wait(timeoutMs?: number): Promise<Signal> {
    return this.waitForTypes(WAIT_TYPES, timeoutMs);
  }

  /** Resolve on the next Signal of the given type. */
  async waitFor(type: SignalType, timeoutMs?: number): Promise<Signal> {
    return this.waitForTypes(new Set([type]), timeoutMs);
  }

  private async waitForTypes(
    types: ReadonlySet<SignalType>,
    timeoutMs?: number,
  ): Promise<Signal> {
    // Serve from the buffer first  -  even when the Pathway has since
    // closed. With an in-process synapse the terminal Signal can arrive
    // (and auto-close the Pathway) before the dispatcher's first wait()
    // runs; already-delivered Signals must remain consumable.
    for (let i = 0; i < this.buffered.length; i++) {
      const sig = this.buffered[i]!;
      if (types.has(sig.type)) {
        this.buffered.splice(i, 1);
        return sig;
      }
    }
    if (this.closed_) {
      throw new PathwayClosedError(`Pathway for trace '${this.traceId}' is closed`);
    }
    return new Promise<Signal>((resolve, reject) => {
      const waiter: Waiter = { types, resolve, reject, settled: false };
      let timer: ReturnType<typeof setTimeout> | null = null;
      const settle =
        <A,>(fn: (a: A) => void) =>
        (a: A): void => {
          if (waiter.settled) return;
          waiter.settled = true;
          if (timer !== null) clearTimeout(timer);
          this.waiters = this.waiters.filter((w) => w !== waiter);
          fn(a);
        };
      waiter.resolve = settle(resolve);
      waiter.reject = settle(reject);
      if (timeoutMs !== undefined) {
        // Deliberately NOT unref'd: a bounded wait must keep the event
        // loop alive until it resolves or times out.
        timer = setTimeout(() => {
          const err = new Error(
            `Pathway.wait timed out after ${timeoutMs}ms on trace '${this.traceId}'`,
          );
          err.name = "TimeoutError";
          waiter.reject(err);
        }, timeoutMs);
      }
      this.waiters.push(waiter);
    });
  }

  // -- consumer shape #2: callbacks ----------------------------------

  /** Register a callback fired for each Signal of the given type. */
  on(type: SignalType, fn: PathwaySignalHandler): PathwaySignalHandler {
    const list = this.handlers.get(type) ?? [];
    list.push(fn);
    this.handlers.set(type, list);
    return fn;
  }

  // -- consumer shape #3: async iteration ----------------------------

  [Symbol.asyncIterator](): AsyncIterator<Signal> {
    return {
      next: (): Promise<IteratorResult<Signal>> => {
        if (this.iterPush.length > 0) {
          const v = this.iterPush.shift()!;
          return Promise.resolve(
            v === null ? { value: undefined, done: true } : { value: v, done: false },
          );
        }
        if (this.closed_) {
          return Promise.resolve({ value: undefined, done: true });
        }
        return new Promise((resolve) => this.iterPull.push(resolve));
      },
    };
  }

  private iterEmit(v: Signal | null): void {
    const pull = this.iterPull.shift();
    if (pull) {
      pull(v === null ? { value: undefined, done: true } : { value: v, done: false });
    } else {
      this.iterPush.push(v);
    }
  }

  // -- lifecycle ------------------------------------------------------

  /** Close the Pathway. Idempotent. Pending waits reject with
   *  PathwayClosedError; iteration completes; the onClose hook fires once. */
  async close(): Promise<void> {
    if (this.closed_) return;
    this.closed_ = true;

    for (const w of [...this.waiters]) {
      w.reject(
        new PathwayClosedError(
          `Pathway for trace '${this.traceId}' closed before a matching Signal arrived`,
        ),
      );
    }
    this.waiters = [];
    this.iterEmit(null);

    if (this.onCloseHook) {
      try {
        await this.onCloseHook(this);
      } catch {
        /* teardown must not throw */
      }
    }
  }

  /** `await using pathway = ...` support. */
  async [Symbol.asyncDispose](): Promise<void> {
    await this.close();
  }

  // -- internal: signal delivery (called by the owning Dendrite) ------

  /** @internal */
  async _deliver(signal: Signal): Promise<void> {
    if (this.closed_) return;

    // Scope filter: drop scoped-out types from wait()/iteration  -  but an
    // explicitly registered callback is an explicit expression of interest,
    // so fire those (this is what lets dispatchOffer(scope:"terminal")
    // collect BIDs), and let terminal types still auto-close.
    if (this.scopeFilter !== null && !this.scopeFilter.has(signal.type)) {
      await this.fireHandlers(signal);
      if (TERMINAL_TYPES.has(signal.type)) await this.close();
      return;
    }

    // 1. Resolve matching waiters (broadcast). Buffer if none consumed.
    let consumed = false;
    for (const w of [...this.waiters]) {
      if (w.types.has(signal.type)) {
        w.resolve(signal);
        consumed = true;
      }
    }
    if (!consumed) this.buffered.push(signal);

    // 2. Per-type callbacks (errors logged, not propagated).
    await this.fireHandlers(signal);

    // 3. Iteration queue.
    this.iterEmit(signal);

    // 4. Auto-close on terminal types.
    if (TERMINAL_TYPES.has(signal.type)) await this.close();
  }

  private async fireHandlers(signal: Signal): Promise<void> {
    for (const h of this.handlers.get(signal.type) ?? []) {
      try {
        await h(signal);
      } catch {
        /* one buggy handler must not break delivery to the others */
      }
    }
  }
}
