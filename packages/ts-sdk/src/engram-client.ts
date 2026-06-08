/**
 * @cosmonapse/sdk  -  Engram caller-side client
 *
 * Ported from `cosmonapse.engram.client`. EngramClient is the caller-side
 * bridge: it builds RECALL / IMPRINT envelopes, publishes them, registers
 * pending promises keyed by the envelope id, and resolves them when a matching
 * RECALLED / IMPRINTED arrives (correlated by `parent_id`). It enforces
 * per-call deadlines and cancels in-flight calls when a TASK terminates.
 *
 * To avoid an import cycle with the Dendrite, the client depends only on a
 * minimal {@link EngramPublisher} (the Dendrite implements it by passing
 * itself in). The Dendrite owns the subscription to RECALLED / IMPRINTED and
 * calls `deliver(signal)` for each inbound.
 */

import {
  directedTo,
  SignalType,
  type Json,
  type Signal,
} from "./envelope.js";
import {
  EngramCancelled,
  EngramTimeout,
  type EngramBinding,
  type Hit,
  type ImprintReceipt,
  type ImprintOp,
  type RecallMode,
  type RecallResult,
} from "./engram.js";
import { imprintSignal, recallSignal } from "./signals.js";

/** The slice of the Dendrite the client needs: a way to put a Signal on the wire. */
export interface EngramPublisher {
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

interface PendingRecall {
  deferred: Deferred<RecallResult>;
  mode: RecallMode;
  timer: Timer | null;
  done: boolean;
  hitsSoFar: Hit[];
  engrams: string[];
}

interface PendingImprint {
  deferred: Deferred<ImprintReceipt | null>;
  timer: Timer | null;
  done: boolean;
}

export interface RecallCallArgs {
  binding?: EngramBinding;
  engramId?: string;
  engramKind?: string;
  query: Json;
  filters?: Json;
  contextRef?: string;
  deadlineMs?: number;
  recallMode?: RecallMode;
  minConfidence?: number;
  traceId: string;
  parentId: string;
  meta?: Json;
}

export interface ImprintCallArgs {
  binding?: EngramBinding;
  engramId?: string;
  engramKind?: string;
  op: ImprintOp;
  entry: Json;
  mergeKey?: string;
  awaitAck?: boolean;
  deadlineMs?: number;
  traceId: string;
  parentId: string;
  meta?: Json;
}

export class EngramClient {
  private pendingRecalls = new Map<string, PendingRecall>();
  private pendingImprints = new Map<string, PendingImprint>();
  private byTrace = new Map<string, Set<string>>();

  constructor(private readonly publisher: EngramPublisher) {}

  async recall(args: RecallCallArgs): Promise<RecallResult> {
    let engramId = args.engramId;
    let engramKind = args.engramKind;
    let deadlineMs = args.deadlineMs;
    let recallMode = args.recallMode;
    if (args.binding) {
      engramId = engramId ?? args.binding.directedId ?? undefined;
      engramKind = engramKind ?? args.binding.directedType ?? undefined;
      if (deadlineMs === undefined) deadlineMs = args.binding.defaultDeadlineMs ?? undefined;
      if (recallMode === undefined) recallMode = args.binding.defaultRecallMode;
    }
    const mode: RecallMode = recallMode ?? "first";

    const sig = recallSignal({
      traceId: args.traceId,
      parentId: args.parentId,
      directed: directedTo(engramId ?? null, { type: engramKind ?? null }),
      query: args.query,
      ...(args.filters !== undefined ? { filters: args.filters } : {}),
      ...(args.contextRef !== undefined ? { contextRef: args.contextRef } : {}),
      ...(deadlineMs !== undefined ? { deadlineMs } : {}),
      ...(args.minConfidence !== undefined ? { minConfidence: args.minConfidence } : {}),
      recallMode: mode,
      ...(args.meta !== undefined ? { meta: args.meta } : {}),
    });

    const d = deferred<RecallResult>();
    const pending: PendingRecall = { deferred: d, mode, timer: null, done: false, hitsSoFar: [], engrams: [] };
    this.pendingRecalls.set(sig.id, pending);
    this.track(args.traceId, sig.id);

    if (deadlineMs !== undefined && deadlineMs > 0) {
      pending.timer = setTimeout(() => this.onRecallDeadline(sig.id), deadlineMs);
    }

    try {
      await this.publisher.publish(sig);
    } catch (err) {
      this.cleanupRecall(args.traceId, sig.id);
      throw err;
    }

    try {
      return await d.promise;
    } finally {
      this.cleanupRecall(args.traceId, sig.id);
    }
  }

  async imprint(args: ImprintCallArgs): Promise<ImprintReceipt | null> {
    let engramId = args.engramId;
    let engramKind = args.engramKind;
    if (args.binding) {
      engramId = engramId ?? args.binding.directedId ?? undefined;
      engramKind = engramKind ?? args.binding.directedType ?? undefined;
    }

    const sig = imprintSignal({
      traceId: args.traceId,
      parentId: args.parentId,
      directed: directedTo(engramId ?? null, { type: engramKind ?? null }),
      op: args.op,
      entry: args.entry,
      ...(args.mergeKey !== undefined ? { mergeKey: args.mergeKey } : {}),
      ...(args.meta !== undefined ? { meta: args.meta } : {}),
    });

    if (!args.awaitAck) {
      await this.publisher.publish(sig);
      return null;
    }

    const d = deferred<ImprintReceipt | null>();
    const pending: PendingImprint = { deferred: d, timer: null, done: false };
    this.pendingImprints.set(sig.id, pending);
    this.track(args.traceId, sig.id);

    if (args.deadlineMs !== undefined && args.deadlineMs > 0) {
      pending.timer = setTimeout(() => this.onImprintDeadline(sig.id), args.deadlineMs);
    }

    try {
      await this.publisher.publish(sig);
    } catch (err) {
      this.cleanupImprint(args.traceId, sig.id);
      throw err;
    }

    try {
      return await d.promise;
    } finally {
      this.cleanupImprint(args.traceId, sig.id);
    }
  }

  /** Match RECALLED / IMPRINTED by parent_id and resolve pendings. */
  deliver(sig: Signal): void {
    const pid = sig.parent_id;
    if (pid === null) return;

    if (sig.type === SignalType.RECALLED) {
      const pending = this.pendingRecalls.get(pid);
      if (pending === undefined) return;
      const hits = hitsFromPayload(sig.payload["hits"]);
      const engramId = typeof sig.payload["engram_id"] === "string" ? (sig.payload["engram_id"] as string) : "";
      const tookMs = typeof sig.payload["took_ms"] === "number" ? (sig.payload["took_ms"] as number) : null;
      const truncated = sig.payload["truncated"] === true;
      if (pending.mode === "first") {
        if (!pending.done) {
          pending.done = true;
          pending.deferred.resolve({
            hits,
            engramIds: engramId ? [engramId] : [],
            truncated,
            tookMs,
          });
        }
      } else {
        pending.hitsSoFar.push(...hits);
        if (engramId) pending.engrams.push(engramId);
      }
    } else if (sig.type === SignalType.IMPRINTED) {
      const pending = this.pendingImprints.get(pid);
      if (pending === undefined || pending.done) return;
      pending.done = true;
      pending.deferred.resolve({
        engramId: typeof sig.payload["engram_id"] === "string" ? (sig.payload["engram_id"] as string) : "",
        op: typeof sig.payload["op"] === "string" ? (sig.payload["op"] as string) : "",
        id: typeof sig.payload["id"] === "string" ? (sig.payload["id"] as string) : null,
        version: typeof sig.payload["version"] === "number" ? (sig.payload["version"] as number) : null,
        tookMs: typeof sig.payload["took_ms"] === "number" ? (sig.payload["took_ms"] as number) : null,
        error: typeof sig.payload["error"] === "string" ? (sig.payload["error"] as string) : null,
        ok: !(typeof sig.payload["error"] === "string"),
      });
    }
  }

  /** Cancel every in-flight recall/imprint on a trace (FINAL/ERROR or shutdown). */
  cancelTrace(traceId: string): void {
    const ids = this.byTrace.get(traceId);
    this.byTrace.delete(traceId);
    if (ids === undefined) return;
    for (const id of ids) {
      const pr = this.pendingRecalls.get(id);
      if (pr !== undefined && !pr.done) {
        pr.done = true;
        if (pr.timer !== null) clearTimeout(pr.timer);
        pr.deferred.reject(new EngramCancelled(`trace ${traceId} terminated while recall ${id} in flight`));
        this.pendingRecalls.delete(id);
      }
      const pi = this.pendingImprints.get(id);
      if (pi !== undefined && !pi.done) {
        pi.done = true;
        if (pi.timer !== null) clearTimeout(pi.timer);
        pi.deferred.reject(new EngramCancelled(`trace ${traceId} terminated while imprint ${id} in flight`));
        this.pendingImprints.delete(id);
      }
    }
  }

  cancelAll(): void {
    for (const traceId of [...this.byTrace.keys()]) this.cancelTrace(traceId);
  }

  private onRecallDeadline(id: string): void {
    const pending = this.pendingRecalls.get(id);
    if (pending === undefined || pending.done) return;
    pending.done = true;
    if (pending.mode === "first") {
      pending.deferred.reject(new EngramTimeout(`RECALL ${id} elapsed deadline without any responder`));
    } else {
      pending.deferred.resolve({
        hits: [...pending.hitsSoFar].sort((a, b) => b.score - a.score),
        engramIds: [...pending.engrams],
        truncated: false,
        tookMs: null,
      });
    }
  }

  private onImprintDeadline(id: string): void {
    const pending = this.pendingImprints.get(id);
    if (pending === undefined || pending.done) return;
    pending.done = true;
    pending.deferred.reject(new EngramTimeout(`IMPRINT ${id} elapsed deadline without IMPRINTED`));
  }

  private track(traceId: string, id: string): void {
    const bucket = this.byTrace.get(traceId);
    if (bucket) bucket.add(id);
    else this.byTrace.set(traceId, new Set([id]));
  }

  private cleanupRecall(traceId: string, id: string): void {
    const p = this.pendingRecalls.get(id);
    if (p?.timer != null) clearTimeout(p.timer);
    this.pendingRecalls.delete(id);
    this.discardTrace(traceId, id);
  }

  private cleanupImprint(traceId: string, id: string): void {
    const p = this.pendingImprints.get(id);
    if (p?.timer != null) clearTimeout(p.timer);
    this.pendingImprints.delete(id);
    this.discardTrace(traceId, id);
  }

  private discardTrace(traceId: string, id: string): void {
    const bucket = this.byTrace.get(traceId);
    if (bucket === undefined) return;
    bucket.delete(id);
    if (bucket.size === 0) this.byTrace.delete(traceId);
  }
}

function hitsFromPayload(raw: unknown): Hit[] {
  if (!Array.isArray(raw)) return [];
  const out: Hit[] = [];
  for (const h of raw) {
    if (h === null || typeof h !== "object") continue;
    const obj = h as Json;
    const entryVal = obj["entry"];
    out.push({
      id: typeof obj["id"] === "string" ? (obj["id"] as string) : "",
      entry: entryVal !== null && typeof entryVal === "object" && !Array.isArray(entryVal)
        ? (entryVal as Json)
        : { value: entryVal },
      score: typeof obj["score"] === "number" ? (obj["score"] as number) : 1.0,
    });
  }
  return out;
}

/** Strict binding lookup. Throws EngramNotBound when the name is unknown. */
export { EngramNotBound } from "./engram.js";
