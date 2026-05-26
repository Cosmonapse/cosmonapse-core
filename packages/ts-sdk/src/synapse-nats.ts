/**
 * @cosmonapse/sdk — NATS synapse adapter
 *
 * NATS maps onto the Synapse contract directly:
 *   - subjects use the same `cosmonapse.<namespace>.<TYPE>` convention
 *   - `*` and `>` wildcards are native NATS — no translation needed
 *   - queue groups are native (`queue` on subscribe)
 *   - request/reply is native (`nc.request`)
 *
 * The `nats` package (nats.js) is **lazy-imported** inside connect(), so this
 * module is safe to load even when `nats` isn't installed. It is declared as
 * an optional dependency:  npm i nats
 *
 * Ported from `cosmonapse.synapse.nats`. One enhancement over the Python
 * adapter: the inbound bridge stashes the NATS reply subject into
 * `signal.meta._reply_to`, and `replyTo()` publishes there — so the SAME
 * request/reply responder code works against MemorySynapse and NatsSynapse.
 */

import { decode, encode, type Signal } from "./envelope.js";
import type {
  MessageHandler,
  RequestOptions,
  SubscribeOptions,
  Subscription,
  Synapse,
} from "./synapse.js";

// Minimal structural types for the slice of the nats.js API we use. We avoid a
// hard `import type "nats"` so type-checking doesn't require the package.
interface NatsMsg {
  data: Uint8Array;
  reply?: string;
  respond(payload?: Uint8Array): boolean;
}
interface NatsSub {
  unsubscribe(): void;
}
interface NatsConnection {
  publish(subject: string, payload: Uint8Array): void;
  subscribe(
    subject: string,
    opts: { queue?: string; callback: (err: Error | null, msg: NatsMsg) => void },
  ): NatsSub;
  request(subject: string, payload: Uint8Array, opts: { timeout: number }): Promise<NatsMsg>;
  drain(): Promise<void>;
  close(): Promise<void>;
}
type NatsModule = {
  connect(opts: { servers: string | string[] }): Promise<NatsConnection>;
};

class NatsSubscription implements Subscription {
  private active = true;
  private readonly sub: NatsSub;
  constructor(sub: NatsSub) {
    this.sub = sub;
  }
  async unsubscribe(): Promise<void> {
    if (this.active) {
      this.sub.unsubscribe();
      this.active = false;
    }
  }
}

export interface NatsSynapseOptions {
  /** NATS server URL or list of URLs. */
  url?: string | string[];
}

/** NATS-backed Synapse. Interchangeable with MemorySynapse. */
export class NatsSynapse implements Synapse {
  private readonly url: string | string[];
  private nc: NatsConnection | null = null;
  private connected = false;

  constructor(opts: NatsSynapseOptions = {}) {
    this.url = opts.url ?? "nats://127.0.0.1:4222";
  }

  async connect(): Promise<void> {
    if (this.connected) return;
    let mod: NatsModule;
    try {
      // Lazy, dynamic import keeps `nats` an optional dependency.
      mod = (await import("nats")) as unknown as NatsModule;
    } catch (err) {
      throw new Error(
        "NatsSynapse requires the 'nats' package. Install it with: npm i nats" +
          (err instanceof Error ? ` (${err.message})` : ""),
      );
    }
    this.nc = await mod.connect({ servers: this.url });
    this.connected = true;
  }

  async close(): Promise<void> {
    if (!this.connected || this.nc === null) return;
    await this.nc.drain();
    this.nc = null;
    this.connected = false;
  }

  private requireConn(method: string): NatsConnection {
    if (!this.connected || this.nc === null) {
      throw new Error(`NatsSynapse.${method} called before connect()`);
    }
    return this.nc;
  }

  async publish(subject: string, signal: Signal): Promise<void> {
    this.requireConn("publish").publish(subject, encode(signal));
  }

  async subscribe(
    subject: string,
    handler: MessageHandler,
    opts: SubscribeOptions = {},
  ): Promise<Subscription> {
    const nc = this.requireConn("subscribe");
    const sub = nc.subscribe(subject, {
      ...(opts.queueGroup !== undefined ? { queue: opts.queueGroup } : {}),
      callback: (err, msg) => {
        if (err) return;
        let signal: Signal;
        try {
          signal = decode(msg.data);
        } catch {
          return; // drop undecodable payloads
        }
        // Cross-adapter parity: expose the native reply subject the same way
        // MemorySynapse does, so `replyTo()` works identically.
        if (msg.reply) {
          signal.meta = { ...signal.meta, _reply_to: msg.reply };
        }
        void Promise.resolve(handler(signal)).catch(() => {
          /* handler errors are isolated */
        });
      },
    });
    return new NatsSubscription(sub);
  }

  async request(subject: string, signal: Signal, opts: RequestOptions = {}): Promise<Signal> {
    const nc = this.requireConn("request");
    const timeout = opts.timeoutMs ?? 5000;
    let msg: NatsMsg;
    try {
      msg = await nc.request(subject, encode(signal), { timeout });
    } catch (err) {
      throw new Error(
        `NatsSynapse: no reply on '${subject}' within ${timeout}ms` +
          (err instanceof Error ? ` (${err.message})` : ""),
      );
    }
    return decode(msg.data);
  }

  /**
   * Send `reply` to the `_reply_to` subject the inbound bridge stashed in
   * `original.meta` (the native NATS inbox). Mirrors MemorySynapse.replyTo so
   * request/reply responder code is adapter-agnostic.
   */
  async replyTo(original: Signal, reply: Signal): Promise<void> {
    const replySubject = original.meta["_reply_to"];
    if (typeof replySubject !== "string" || !replySubject) {
      throw new Error("Signal has no _reply_to in meta — not a request signal");
    }
    await this.publish(replySubject, reply);
  }
}
