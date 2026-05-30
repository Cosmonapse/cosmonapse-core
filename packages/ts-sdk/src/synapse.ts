/**
 * @cosmonapse/sdk — synapse
 *
 * The Synapse interface (five methods) and the in-process MemorySynapse,
 * ported 1:1 from `cosmonapse.synapse.base` / `cosmonapse.synapse.memory`.
 *
 * MemorySynapse is NOT a throwaway test double — it is the adapter that backs
 * the local dev experience, and any code written against it works unchanged
 * against a networked adapter. NatsSynapse is ported (synapse-nats.ts); the
 * Kafka adapter is still outstanding — see PORTING_STATUS.md.
 *
 * Subject convention (ENVELOPE_SPEC.md §10):
 *   cosmonapse.<namespace>.<type>     e.g. cosmonapse.team_a.TASK
 *   cosmonapse.>                      subscribe to everything
 *
 * Wildcards (same as NATS):
 *   *  matches exactly one token
 *   >  matches one or more trailing tokens (must be the last token)
 */

import type { Signal } from "./envelope.js";

/** A subscriber callback. May be sync or async; async handlers are awaited. */
export type MessageHandler = (signal: Signal) => void | Promise<void>;

/** Handle for an active subscription. Used to unsubscribe cleanly. */
export interface Subscription {
  unsubscribe(): Promise<void>;
}

export interface SubscribeOptions {
  /**
   * If set, only one subscriber in the group receives each message
   * (round-robin load balancing). Doppler subscribers must NOT use a group.
   */
  queueGroup?: string;
}

export interface RequestOptions {
  timeoutMs?: number;
}

/** Abstract base for all Cosmonapse synapse adapters. */
export interface Synapse {
  /** Establish connection / initialise in-memory state. */
  connect(): Promise<void>;
  /** Gracefully disconnect and release resources. */
  close(): Promise<void>;
  /** Publish a Signal to the given subject. */
  publish(subject: string, signal: Signal): Promise<void>;
  /** Subscribe to a subject pattern (exact or wildcard). */
  subscribe(
    subject: string,
    handler: MessageHandler,
    opts?: SubscribeOptions,
  ): Promise<Subscription>;
  /** Publish a Signal and wait for exactly one reply. */
  request(subject: string, signal: Signal, opts?: RequestOptions): Promise<Signal>;
}

interface SubEntry {
  id: number;
  group: string | null;
  handler: MessageHandler;
}

class MemorySubscription implements Subscription {
  private active = true;
  private readonly synapse: MemorySynapse;
  private readonly subject: string;
  private readonly handlerId: number;

  constructor(synapse: MemorySynapse, subject: string, handlerId: number) {
    this.synapse = synapse;
    this.subject = subject;
    this.handlerId = handlerId;
  }

  async unsubscribe(): Promise<void> {
    if (this.active) {
      this.synapse._removeHandler(this.subject, this.handlerId);
      this.active = false;
    }
  }
}

/**
 * In-process synapse. Supports fan-out, queue groups (round-robin),
 * wildcard subjects, and request/reply. Single-threaded by design.
 */
export class MemorySynapse implements Synapse {
  private subs = new Map<string, SubEntry[]>();
  private counter = 0;
  private connected = false;
  private rrCounters = new Map<string, number>();

  async connect(): Promise<void> {
    this.connected = true;
  }

  async close(): Promise<void> {
    this.subs.clear();
    this.connected = false;
  }

  private nextId(): number {
    this.counter += 1;
    return this.counter;
  }

  /** @internal */
  _removeHandler(subject: string, handlerId: number): void {
    const entries = this.subs.get(subject);
    if (!entries) return;
    const kept = entries.filter((e) => e.id !== handlerId);
    if (kept.length) this.subs.set(subject, kept);
    else this.subs.delete(subject);
  }

  /**
   * Return true if `subject` matches `pattern`.
   *   *  matches any single token (no dots)
   *   >  matches any sequence of trailing tokens
   */
  static matches(pattern: string, subject: string): boolean {
    if (pattern === subject) return true;
    const p = pattern.split(".");
    const s = subject.split(".");
    let i = 0;
    let j = 0;
    while (i < p.length && j < s.length) {
      if (p[i] === ">") return true;
      if (p[i] === "*") {
        i += 1;
        j += 1;
        continue;
      }
      if (p[i] !== s[j]) return false;
      i += 1;
      j += 1;
    }
    return i === p.length && j === s.length;
  }

  async publish(subject: string, signal: Signal): Promise<void> {
    if (!this.connected) throw new Error("Synapse not connected");
    // Serialise to bytes and back so every subscriber gets an independent
    // copy — mirrors a real wire and prevents shared-reference mutation.
    await this.deliver(subject, signal);
  }

  private async deliver(subject: string, signal: Signal): Promise<void> {
    const queueGroups = new Map<string, MessageHandler[]>();
    const solo: MessageHandler[] = [];

    for (const [pattern, entries] of this.subs) {
      if (!MemorySynapse.matches(pattern, subject)) continue;
      for (const e of entries) {
        if (e.group === null) solo.push(e.handler);
        else {
          const list = queueGroups.get(e.group) ?? [];
          list.push(e.handler);
          queueGroups.set(e.group, list);
        }
      }
    }

    const pending: Array<Promise<unknown>> = [];
    const invoke = (h: MessageHandler): void => {
      try {
        const r = h(structuredClone(signal));
        if (r instanceof Promise) pending.push(r);
      } catch (err) {
        pending.push(Promise.reject(err));
      }
    };

    for (const h of solo) invoke(h);

    // Queue groups: strict round-robin via a per-group counter.
    for (const [group, handlers] of queueGroups) {
      if (!handlers.length) continue;
      const n = this.rrCounters.get(group) ?? 0;
      const idx = n % handlers.length;
      this.rrCounters.set(group, n + 1);
      invoke(handlers[idx]!);
    }

    if (pending.length) await Promise.allSettled(pending);
  }

  async subscribe(
    subject: string,
    handler: MessageHandler,
    opts: SubscribeOptions = {},
  ): Promise<Subscription> {
    if (!this.connected) throw new Error("Synapse not connected");
    const id = this.nextId();
    const entries = this.subs.get(subject) ?? [];
    entries.push({ id, group: opts.queueGroup ?? null, handler });
    this.subs.set(subject, entries);
    return new MemorySubscription(this, subject, id);
  }

  async request(
    subject: string,
    signal: Signal,
    opts: RequestOptions = {},
  ): Promise<Signal> {
    if (!this.connected) throw new Error("Synapse not connected");
    const timeoutMs = opts.timeoutMs ?? 5000;
    const replySubject = `_INBOX.${signal.id}`;

    let resolveFn!: (s: Signal) => void;
    let settled = false;
    const fut = new Promise<Signal>((resolve) => {
      resolveFn = resolve;
    });

    const sub = await this.subscribe(replySubject, (reply) => {
      if (!settled) {
        settled = true;
        resolveFn(reply);
      }
    });

    try {
      // Attach reply-to in meta so the receiver knows where to respond.
      const enriched: Signal = {
        ...signal,
        meta: { ...signal.meta, _reply_to: replySubject },
      };
      await this.publish(subject, enriched);

      return await new Promise<Signal>((resolve, reject) => {
        const timer = setTimeout(() => {
          if (!settled) {
            settled = true;
            reject(new Error(`No reply received on '${replySubject}' within ${timeoutMs}ms`));
          }
        }, timeoutMs);
        fut.then((s) => {
          clearTimeout(timer);
          resolve(s);
        });
      });
    } finally {
      await sub.unsubscribe();
    }
  }

  /**
   * Convenience: send `reply` to the `_reply_to` subject stored in
   * `original.meta`. Used by request/reply responders.
   */
  async replyTo(original: Signal, reply: Signal): Promise<void> {
    const replySubject = original.meta["_reply_to"];
    if (typeof replySubject !== "string" || !replySubject) {
      throw new Error("Signal has no _reply_to in meta — not a request signal");
    }
    await this.publish(replySubject, reply);
  }
}
