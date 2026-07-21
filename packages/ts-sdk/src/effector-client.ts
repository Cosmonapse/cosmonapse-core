/**
 * @cosmonapse/sdk  -  Effector caller-side client
 *
 * Ported from `cosmonapse.effector.client`. EffectorClient is the caller-side
 * bridge for tool I/O - the action-side twin of {@link EngramClient}, built
 * the way that module promised any request/reply client could be. The Axon
 * (native tool calls, the injected `callTool` helper) and orchestrating
 * Dendrites both call into it; only the Dendrite is allowed to touch the
 * Synapse.
 *
 * It builds a TOOL_CALL envelope, publishes it, registers a pending promise
 * keyed by the envelope id, and resolves it when the matching TOOL_RESULT
 * arrives (correlated by `parent_id`). It enforces per-call deadlines
 * ({@link EffectorTimeout}) and cancels in-flight calls when the containing
 * TASK terminates or the Dendrite shuts down ({@link EffectorCancelled}).
 */

import { directedTo, SignalType, type Json, type Signal } from "./envelope.js";
import {
  EffectorCancelled,
  EffectorTimeout,
  ToolOutcome,
  type EffectorBinding,
} from "./effector.js";
import { toolCallSignal } from "./signals.js";

/** The slice of the Dendrite the client needs: a way to put a Signal on the wire. */
export interface EffectorPublisher {
  publish(signal: Signal): Promise<void>;
}

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (err: unknown) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (err: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

type Timer = ReturnType<typeof setTimeout>;

interface PendingCall {
  deferred: Deferred<ToolOutcome>;
  tool: string;
  timer: Timer | null;
  done: boolean;
}

export interface ToolCallArgs {
  binding?: EffectorBinding;
  effectorId?: string;
  effectorKind?: string;
  tool: string;
  args?: Json;
  callId?: string;
  deadlineMs?: number;
  traceId: string;
  parentId: string;
  /** Accepted for caller observability; not part of the envelope addressing
   *  (a TOOL_CALL's `directed` addresses the target Effector, not the
   *  producer). */
  neuron?: string;
  meta?: Json;
}

export class EffectorClient {
  private readonly pendingCalls = new Map<string, PendingCall>();
  private readonly byTrace = new Map<string, Set<string>>();

  constructor(private readonly publisher: EffectorPublisher) {}

  /**
   * Emit TOOL_CALL, await the matching TOOL_RESULT, return the outcome.
   *
   * With no `deadlineMs` (and none on the binding) the call waits until the
   * trace terminates - callers that must not hang pass a deadline.
   */
  async call(args: ToolCallArgs): Promise<ToolOutcome> {
    let effectorId = args.effectorId;
    let effectorKind = args.effectorKind;
    let deadlineMs = args.deadlineMs;
    if (args.binding) {
      effectorId = effectorId ?? args.binding.directedId ?? undefined;
      effectorKind = effectorKind ?? args.binding.directedType ?? undefined;
      if (deadlineMs === undefined) deadlineMs = args.binding.defaultDeadlineMs ?? undefined;
    }

    const sig = toolCallSignal({
      traceId: args.traceId,
      parentId: args.parentId,
      directed: directedTo(effectorId ?? null, { type: effectorKind ?? null }),
      tool: args.tool,
      args: args.args ?? {},
      ...(args.callId !== undefined ? { callId: args.callId } : {}),
      ...(args.meta !== undefined ? { meta: args.meta } : {}),
    });

    const d = deferred<ToolOutcome>();
    const pending: PendingCall = { deferred: d, tool: args.tool, timer: null, done: false };
    this.pendingCalls.set(sig.id, pending);
    this.track(args.traceId, sig.id);

    if (deadlineMs !== undefined && deadlineMs > 0) {
      pending.timer = setTimeout(() => this.onDeadline(sig.id), deadlineMs);
    }

    try {
      await this.publisher.publish(sig);
    } catch (err) {
      this.cleanup(args.traceId, sig.id);
      throw err;
    }

    try {
      return await d.promise;
    } finally {
      this.cleanup(args.traceId, sig.id);
    }
  }

  /** Match TOOL_RESULT by parent_id and resolve the pending call. */
  deliver(sig: Signal): void {
    if (sig.type !== SignalType.TOOL_RESULT) return;
    const pid = sig.parent_id;
    if (pid === null) return;
    const pending = this.pendingCalls.get(pid);
    if (pending === undefined || pending.done) return;
    pending.done = true;
    // The answering Effector is read off the reply's directed attribution.
    pending.deferred.resolve(
      new ToolOutcome({
        tool: typeof sig.payload["tool"] === "string" ? (sig.payload["tool"] as string) : pending.tool,
        result: sig.payload["result"] ?? null,
        error: typeof sig.payload["error"] === "string" ? (sig.payload["error"] as string) : null,
        callId: typeof sig.payload["call_id"] === "string" ? (sig.payload["call_id"] as string) : null,
        tookMs: typeof sig.payload["took_ms"] === "number" ? (sig.payload["took_ms"] as number) : null,
        effectorId: sig.directed?.id ?? null,
      }),
    );
  }

  /** Cancel every in-flight tool call on a trace (FINAL/ERROR or shutdown). */
  cancelTrace(traceId: string): void {
    const ids = this.byTrace.get(traceId);
    this.byTrace.delete(traceId);
    if (ids === undefined) return;
    for (const id of ids) {
      const pc = this.pendingCalls.get(id);
      if (pc !== undefined && !pc.done) {
        pc.done = true;
        if (pc.timer !== null) clearTimeout(pc.timer);
        pc.deferred.reject(
          new EffectorCancelled(`trace ${traceId} terminated while TOOL_CALL ${id} was in flight`),
        );
        this.pendingCalls.delete(id);
      }
    }
  }

  cancelAll(): void {
    for (const traceId of [...this.byTrace.keys()]) this.cancelTrace(traceId);
  }

  private onDeadline(id: string): void {
    const pending = this.pendingCalls.get(id);
    if (pending === undefined || pending.done) return;
    pending.done = true;
    pending.deferred.reject(
      new EffectorTimeout("TOOL_CALL elapsed its deadline without TOOL_RESULT"),
    );
  }

  private track(traceId: string, id: string): void {
    const bucket = this.byTrace.get(traceId);
    if (bucket) bucket.add(id);
    else this.byTrace.set(traceId, new Set([id]));
  }

  private cleanup(traceId: string, id: string): void {
    const p = this.pendingCalls.get(id);
    if (p?.timer != null) clearTimeout(p.timer);
    this.pendingCalls.delete(id);
    const bucket = this.byTrace.get(traceId);
    if (bucket !== undefined) {
      bucket.delete(id);
      if (bucket.size === 0) this.byTrace.delete(traceId);
    }
  }
}

/** Strict binding lookup error - re-exported for symmetry with EngramClient. */
export { EffectorNotBound } from "./effector.js";
