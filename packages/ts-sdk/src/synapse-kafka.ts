/**
 * @cosmonapse/sdk  -  Kafka synapse adapter
 *
 * Ported from `cosmonapse.synapse.kafka`.
 *
 * Kafka does not map onto the Synapse contract as cleanly as NATS  -  a few
 * translations are needed:
 *
 *   Cosmonapse                    Kafka
 *   --------------------------    ----------------------------------
 *   subject `a.b.TYPE`            topic `a.b.TYPE` (verbatim)
 *   wildcard `a.b.*` / `a.>`      regex consumer subscription
 *                                 (`^a\.b\.[^.]+$` / `^a\..+$`)
 *   queueGroup="workers"         consumer `groupId="workers"`
 *   no queueGroup (Doppler)      consumer `groupId=<unique>` so it joins its
 *                                 own group and sees every message
 *   request / reply              per-call reply topic; the requester
 *                                 subscribes to its inbox before publishing,
 *                                 then awaits one message whose parent_id
 *                                 matches the request's signal id.
 *
 * The `kafkajs` library is **lazy-imported**  -  the module loads fine without it;
 * `connect()` raises a clear error if it is missing. Install:  npm i kafkajs
 *
 * Caveats
 * -------
 * - Kafka topics must exist (`auto.create.topics.enable=true` on the broker if
 *   you want them created on first publish).
 * - High-fan-out request/reply is poorly suited to Kafka. Prefer NATS for that;
 *   use Kafka where you want the long-term audit log of every Signal.
 */

import { decode, encode, type Signal } from "./envelope.js";
import type {
  MessageHandler,
  RequestOptions,
  SubscribeOptions,
  Subscription,
  Synapse,
} from "./synapse.js";

// Minimal structural types for the slice of kafkajs we use, so type-checking
// does not require the package to be installed.
interface KafkaProducer {
  connect(): Promise<void>;
  disconnect(): Promise<void>;
  send(args: { topic: string; messages: Array<{ value: Buffer | Uint8Array }> }): Promise<unknown>;
}
interface KafkaConsumerMessage {
  value: Buffer | null;
}
interface KafkaConsumer {
  connect(): Promise<void>;
  disconnect(): Promise<void>;
  subscribe(args: { topic: string | RegExp; fromBeginning?: boolean }): Promise<void>;
  run(args: {
    eachMessage: (payload: { topic: string; message: KafkaConsumerMessage }) => Promise<void>;
  }): Promise<void>;
}
interface KafkaClient {
  producer(): KafkaProducer;
  consumer(args: { groupId: string }): KafkaConsumer;
}
interface KafkaModule {
  Kafka: new (args: { brokers: string[]; clientId?: string }) => KafkaClient;
}

/**
 * Convert a Cosmonapse subject (with optional `*` / `>` wildcards) into a Kafka
 * topic RegExp. Returns null if the subject is not a wildcard pattern (caller
 * should subscribe to the exact topic).
 */
function subjectToTopicRegex(pattern: string): RegExp | null {
  if (!pattern.includes("*") && !pattern.includes(">")) return null;
  const escape = (s: string): string => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const parts = pattern.split(".").map((tok) => {
    if (tok === "*") return "[^.]+";
    if (tok === ">") return ".+";
    return escape(tok);
  });
  return new RegExp("^" + parts.join("\\.") + "$");
}

class KafkaSubscription implements Subscription {
  private active = true;
  constructor(private readonly consumer: KafkaConsumer) {}
  async unsubscribe(): Promise<void> {
    if (!this.active) return;
    this.active = false;
    try {
      await this.consumer.disconnect();
    } catch {
      /* best-effort */
    }
  }
}

export interface KafkaSynapseOptions {
  /** Kafka broker(s), e.g. "localhost:9092" or a list. */
  bootstrapServers?: string | string[];
  /** Optional client identifier. */
  clientId?: string;
}

/** Kafka-backed Synapse. Interchangeable with MemorySynapse. */
export class KafkaSynapse implements Synapse {
  private readonly brokers: string[];
  private readonly clientId: string | undefined;
  private kafka: KafkaClient | null = null;
  private producer: KafkaProducer | null = null;
  private readonly consumers: KafkaConsumer[] = [];
  private connected = false;

  constructor(opts: KafkaSynapseOptions = {}) {
    const bs = opts.bootstrapServers ?? "localhost:9092";
    this.brokers = Array.isArray(bs) ? bs : [bs];
    this.clientId = opts.clientId;
  }

  async connect(): Promise<void> {
    if (this.connected) return;
    let mod: KafkaModule;
    try {
      mod = (await import("kafkajs")) as unknown as KafkaModule;
    } catch (err) {
      throw new Error(
        "KafkaSynapse requires the 'kafkajs' package. Install it with: npm i kafkajs" +
          (err instanceof Error ? ` (${err.message})` : ""),
      );
    }
    this.kafka = new mod.Kafka({
      brokers: this.brokers,
      ...(this.clientId !== undefined ? { clientId: this.clientId } : {}),
    });
    this.producer = this.kafka.producer();
    await this.producer.connect();
    this.connected = true;
  }

  async close(): Promise<void> {
    if (!this.connected) return;
    for (const c of this.consumers) {
      try {
        await c.disconnect();
      } catch {
        /* best-effort */
      }
    }
    this.consumers.length = 0;
    if (this.producer !== null) {
      await this.producer.disconnect();
      this.producer = null;
    }
    this.kafka = null;
    this.connected = false;
  }

  async publish(subject: string, signal: Signal): Promise<void> {
    if (!this.connected || this.producer === null) {
      throw new Error("KafkaSynapse.publish called before connect()");
    }
    await this.producer.send({ topic: subject, messages: [{ value: encode(signal) }] });
  }

  async subscribe(
    subject: string,
    handler: MessageHandler,
    opts: SubscribeOptions = {},
  ): Promise<Subscription> {
    if (!this.connected || this.kafka === null) {
      throw new Error("KafkaSynapse.subscribe called before connect()");
    }
    // No queueGroup => the Doppler pattern; a unique groupId so the consumer
    // joins its own group and sees every record.
    const groupId =
      opts.queueGroup ??
      `cosmonapse-solo-${Math.random().toString(36).slice(2, 14)}`;

    const consumer = this.kafka.consumer({ groupId });
    await consumer.connect();

    const topicRegex = subjectToTopicRegex(subject);
    await consumer.subscribe(
      topicRegex !== null
        ? { topic: topicRegex, fromBeginning: false }
        : { topic: subject, fromBeginning: false },
    );

    await consumer.run({
      eachMessage: async ({ message }) => {
        if (message.value === null) return;
        let signal: Signal;
        try {
          signal = decode(message.value);
        } catch {
          return;
        }
        try {
          await handler(signal);
        } catch {
          /* handler errors are isolated */
        }
      },
    });

    this.consumers.push(consumer);
    return new KafkaSubscription(consumer);
  }

  async request(
    subject: string,
    signal: Signal,
    opts: RequestOptions = {},
  ): Promise<Signal> {
    if (!this.connected) throw new Error("KafkaSynapse.request called before connect()");
    const timeoutMs = opts.timeoutMs ?? 5000;
    const replyTopic = `_inbox.${signal.id}`;

    let settled = false;
    let resolveFn!: (s: Signal) => void;
    const fut = new Promise<Signal>((resolve) => {
      resolveFn = resolve;
    });

    const sub = await this.subscribe(replyTopic, (reply) => {
      if (!settled && reply.parent_id === signal.id) {
        settled = true;
        resolveFn(reply);
      }
    });

    try {
      const enriched: Signal = {
        ...signal,
        meta: { ...signal.meta, _reply_to: replyTopic },
      };
      await this.publish(subject, enriched);
      return await new Promise<Signal>((resolve, reject) => {
        const timer = setTimeout(() => {
          if (!settled) {
            settled = true;
            reject(new Error(`KafkaSynapse: no reply on '${replyTopic}' within ${timeoutMs}ms`));
          }
        }, timeoutMs);
        void fut.then((s) => {
          clearTimeout(timer);
          resolve(s);
        });
      });
    } finally {
      await sub.unsubscribe();
    }
  }
}
