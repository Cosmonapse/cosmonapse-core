/**
 * @cosmonapse/sdk — local dev synapse
 *
 * A tiny TCP + NDJSON broker, ported 1:1 from `cosmonapse.synapse.dev`.
 *
 * `cosmo synapse start memory` boots a {@link DevSynapseServer} and prints a URL
 * like `cosmo://127.0.0.1:7070`. Any process can then connect to that URL with
 * `const synapse = await connectSynapse("cosmo://...")` and hand the result to a
 * `new Dendrite({ synapse, namespace })` to start exchanging Signals.
 *
 * This is **not** a production synapse. It is the equivalent of MemorySynapse
 * for the case where Axons, Dendrites and Cortices live in separate processes
 * on a developer's laptop. For production use NatsSynapse or KafkaSynapse.
 *
 * Wire protocol (NDJSON over TCP, UTF-8, `\n` framed) — identical to the Python
 * server so the two interoperate:
 *
 *   Client -> Server:
 *     {"op":"hello"} | {"op":"ping"}
 *     {"op":"pub","subject":"a.b.c","frame":"<utf8 JSON of Signal>"}
 *     {"op":"sub","sub_id":"s1","subject":"a.b.*","queue_group":null}
 *     {"op":"unsub","sub_id":"s1"}
 *     {"op":"ns_register","namespace":"dev","transport":"memory"}
 *     {"op":"mgmt_list"} | {"op":"mgmt_info","namespace":"dev"} | {"op":"mgmt_stop","namespace":"dev"}
 *
 *   Server -> Client:
 *     {"op":"welcome"} | {"op":"pong"}
 *     {"op":"msg","sub_id":"s1","subject":"a.b.c","frame":"<utf8 JSON>"}
 *     {"op":"err","message":"..."}
 *     {"op":"ns_registered","namespace":"dev"}
 *     {"op":"mgmt_ns_list","namespaces":[...]} | {"op":"mgmt_ns_info",...}
 *     {"op":"mgmt_stop_ack","namespace":"dev"} | {"op":"ns_stopping","namespace":"dev"}
 *
 * Subject matching uses the same `*` / `>` wildcards as MemorySynapse and NATS.
 * Queue groups load-balance round-robin within the group; subscribers with no
 * queue_group (Dopplers) each receive every matching message.
 */

import { createConnection, createServer, type Server, type Socket } from "node:net";
import { decode, encode, type Signal } from "./envelope.js";
import { MemorySynapse } from "./synapse.js";
import type {
  MessageHandler,
  RequestOptions,
  SubscribeOptions,
  Subscription,
  Synapse,
} from "./synapse.js";

/** Shared subject matcher — same semantics as MemorySynapse.matches. */
function matches(pattern: string, subject: string): boolean {
  return MemorySynapse.matches(pattern, subject);
}

/** Split a Buffer/string stream into `\n`-framed lines. */
class LineSplitter {
  private buf = "";
  push(chunk: Buffer | string): string[] {
    this.buf += chunk.toString("utf-8");
    const lines: string[] = [];
    let idx: number;
    while ((idx = this.buf.indexOf("\n")) !== -1) {
      lines.push(this.buf.slice(0, idx));
      this.buf = this.buf.slice(idx + 1);
    }
    return lines;
  }
}

type WireOp = Record<string, unknown> & { op?: string };

interface NamespaceInfo {
  transport: string;
  started_at: string;
  signal_count: number;
  owner: ClientSession;
}

// ---------------------------------------------------------------------------
// Server-side
// ---------------------------------------------------------------------------

class ClientSession {
  readonly subs = new Map<string, { subject: string; queueGroup: string | null }>();
  alive = true;
  private writeChain: Promise<void> = Promise.resolve();

  constructor(
    readonly socket: Socket,
    readonly peer: string,
  ) {}

  send(payload: WireOp): Promise<void> {
    if (!this.alive) return Promise.resolve();
    const line = JSON.stringify(payload) + "\n";
    // Serialise writes so frames never interleave.
    this.writeChain = this.writeChain.then(
      () =>
        new Promise<void>((resolve) => {
          if (!this.alive) return resolve();
          this.socket.write(line, () => resolve());
        }),
    );
    return this.writeChain;
  }

  close(): void {
    this.alive = false;
    try {
      this.socket.destroy();
    } catch {
      /* ignore */
    }
  }
}

export interface DevSynapseServerOptions {
  host?: string;
  port?: number;
}

/**
 * TCP server speaking the dev-synapse wire protocol.
 *
 * ```ts
 * const server = new DevSynapseServer({ host: "127.0.0.1", port: 7070 });
 * await server.start();
 * console.log(server.url); // cosmo://127.0.0.1:7070
 * // ...
 * await server.stop();
 * ```
 */
export class DevSynapseServer {
  private _host: string;
  private _port: number;
  private server: Server | null = null;
  private readonly sessions = new Set<ClientSession>();
  private readonly rrCounters = new Map<string, number>();
  private readonly namespaces = new Map<string, NamespaceInfo>();

  /**
   * Optional observer hook: every published Signal is passed here as
   * (subject, frame) before fan-out. Used by `cosmo synapse start` to stream
   * to stdout.
   */
  onSignal: ((subject: string, frame: string) => void) | null = null;

  constructor(opts: DevSynapseServerOptions = {}) {
    this._host = opts.host ?? "127.0.0.1";
    this._port = opts.port ?? 7070;
  }

  get host(): string {
    return this._host;
  }
  get port(): number {
    return this._port;
  }
  get url(): string {
    return `cosmo://${this._host}:${this._port}`;
  }
  get sessionCount(): number {
    return this.sessions.size;
  }

  async start(): Promise<void> {
    if (this.server !== null) return;
    await new Promise<void>((resolve, reject) => {
      const server = createServer((socket) => this.handleClient(socket));
      server.once("error", reject);
      server.listen(this._port, this._host, () => {
        const addr = server.address();
        if (addr && typeof addr === "object") this._port = addr.port;
        server.off("error", reject);
        this.server = server;
        resolve();
      });
    });
  }

  async stop(): Promise<void> {
    if (this.server === null) return;
    for (const s of [...this.sessions]) s.close();
    this.sessions.clear();
    const server = this.server;
    this.server = null;
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }

  private handleClient(socket: Socket): void {
    socket.setNoDelay(true);
    const peerAddr = socket.remoteAddress ?? "?";
    const peerPort = socket.remotePort ?? 0;
    const session = new ClientSession(socket, `${peerAddr}:${peerPort}`);
    this.sessions.add(session);
    const splitter = new LineSplitter();

    void session.send({ op: "welcome" });

    socket.on("data", (chunk) => {
      for (const line of splitter.push(chunk)) {
        if (!line.trim()) continue;
        let msg: WireOp;
        try {
          msg = JSON.parse(line) as WireOp;
        } catch (err) {
          void session.send({ op: "err", message: `bad JSON: ${String(err)}` });
          continue;
        }
        void this.handleOp(session, msg);
      }
    });

    const cleanup = (): void => {
      this.sessions.delete(session);
      session.close();
      for (const [ns, info] of [...this.namespaces]) {
        if (info.owner === session) this.namespaces.delete(ns);
      }
    };
    socket.on("close", cleanup);
    socket.on("error", cleanup);
  }

  private async handleOp(session: ClientSession, msg: WireOp): Promise<void> {
    const op = msg.op;
    switch (op) {
      case "pub": {
        const subject = msg["subject"] as string | undefined;
        const frame = msg["frame"] as string | undefined;
        if (!subject || frame === undefined) {
          await session.send({ op: "err", message: "pub: missing subject/frame" });
          return;
        }
        await this.deliver(subject, frame);
        return;
      }
      case "sub": {
        const subId = msg["sub_id"] as string | undefined;
        const subject = msg["subject"] as string | undefined;
        const queueGroup = (msg["queue_group"] as string | null | undefined) ?? null;
        if (!subId || !subject) {
          await session.send({ op: "err", message: "sub: missing sub_id/subject" });
          return;
        }
        session.subs.set(subId, { subject, queueGroup });
        return;
      }
      case "unsub": {
        const subId = msg["sub_id"] as string | undefined;
        if (subId) session.subs.delete(subId);
        return;
      }
      case "hello":
        await session.send({ op: "welcome" });
        return;
      case "ping":
        await session.send({ op: "pong" });
        return;
      case "ns_register": {
        const namespace = msg["namespace"] as string | undefined;
        const transport = (msg["transport"] as string | undefined) ?? "memory";
        if (!namespace) {
          await session.send({ op: "err", message: "ns_register: missing namespace" });
          return;
        }
        this.namespaces.set(namespace, {
          transport,
          started_at: new Date().toISOString(),
          signal_count: 0,
          owner: session,
        });
        await session.send({ op: "ns_registered", namespace });
        return;
      }
      case "mgmt_list": {
        const nsList = [...this.namespaces.entries()].map(([namespace, info]) => ({
          namespace,
          transport: info.transport,
          started_at: info.started_at,
          signal_count: info.signal_count,
        }));
        await session.send({ op: "mgmt_ns_list", namespaces: nsList });
        return;
      }
      case "mgmt_info": {
        const namespace = msg["namespace"] as string | undefined;
        const info = namespace ? this.namespaces.get(namespace) : undefined;
        if (!namespace || !info) {
          await session.send({ op: "err", message: `namespace '${String(namespace)}' not found` });
          return;
        }
        await session.send({
          op: "mgmt_ns_info",
          namespace,
          transport: info.transport,
          started_at: info.started_at,
          signal_count: info.signal_count,
          client_count: this.sessions.size,
        });
        return;
      }
      case "mgmt_stop": {
        const namespace = msg["namespace"] as string | undefined;
        const info = namespace ? this.namespaces.get(namespace) : undefined;
        if (!namespace || !info) {
          await session.send({ op: "err", message: `namespace '${String(namespace)}' not found` });
          return;
        }
        this.namespaces.delete(namespace);
        if (info.owner.alive) await info.owner.send({ op: "ns_stopping", namespace });
        await session.send({ op: "mgmt_stop_ack", namespace });
        return;
      }
      default:
        await session.send({ op: "err", message: `unknown op '${String(op)}'` });
    }
  }

  private async deliver(subject: string, frame: string): Promise<void> {
    // Track signal count for the namespace. Subjects follow the convention
    // cosmonapse.<namespace>.<TYPE>[...]
    const parts = subject.split(".");
    if (parts.length >= 2 && parts[0] === "cosmonapse") {
      const info = this.namespaces.get(parts[1]!);
      if (info) info.signal_count += 1;
    }

    if (this.onSignal !== null) {
      try {
        this.onSignal(subject, frame);
      } catch {
        /* observer errors are isolated */
      }
    }

    const solo: Array<[ClientSession, string]> = [];
    const groups = new Map<string, Array<[ClientSession, string]>>();

    for (const session of this.sessions) {
      for (const [subId, { subject: pat, queueGroup }] of session.subs) {
        if (!matches(pat, subject)) continue;
        if (queueGroup === null) solo.push([session, subId]);
        else {
          const list = groups.get(queueGroup) ?? [];
          list.push([session, subId]);
          groups.set(queueGroup, list);
        }
      }
    }

    const base = { op: "msg", subject, frame };
    for (const [session, subId] of solo) await session.send({ ...base, sub_id: subId });

    for (const [group, members] of groups) {
      if (!members.length) continue;
      const n = this.rrCounters.get(group) ?? 0;
      const idx = n % members.length;
      this.rrCounters.set(group, n + 1);
      const [session, subId] = members[idx]!;
      await session.send({ ...base, sub_id: subId });
    }
  }
}

// ---------------------------------------------------------------------------
// Client-side: DevSynapse
// ---------------------------------------------------------------------------

class DevSubscription implements Subscription {
  private active = true;
  constructor(
    private readonly synapse: DevSynapse,
    private readonly subId: string,
  ) {}
  async unsubscribe(): Promise<void> {
    if (!this.active) return;
    this.active = false;
    await this.synapse._sendUnsub(this.subId);
  }
}

export interface DevSynapseOptions {
  host?: string;
  port?: number;
  /** Convenience: pass `cosmo://host:port` instead of host/port. */
  url?: string;
}

/** TCP / NDJSON client speaking to a {@link DevSynapseServer}. */
export class DevSynapse implements Synapse {
  private _host: string;
  private _port: number;
  private socket: Socket | null = null;
  private connected = false;
  private readonly handlers = new Map<string, MessageHandler>();
  private subCounter = 0;
  private readonly splitter = new LineSplitter();

  constructor(opts: DevSynapseOptions = {}) {
    let host = opts.host;
    let port = opts.port;
    if (opts.url !== undefined) {
      const u = new URL(opts.url);
      if (u.protocol !== "cosmo:") {
        throw new Error(`DevSynapse expects scheme cosmo://, got '${u.protocol}'`);
      }
      host = host ?? (u.hostname || "127.0.0.1");
      if (port === undefined) port = u.port ? Number(u.port) : 7070;
    }
    this._host = host ?? "127.0.0.1";
    this._port = port ?? 7070;
  }

  get url(): string {
    return `cosmo://${this._host}:${this._port}`;
  }

  async connect(): Promise<void> {
    if (this.connected) return;
    const socket = await new Promise<Socket>((resolve, reject) => {
      const s = createConnection({ host: this._host, port: this._port }, () => {
        s.off("error", reject);
        resolve(s);
      });
      s.once("error", reject);
    });
    socket.setNoDelay(true);
    this.socket = socket;
    this.connected = true;

    socket.on("data", (chunk) => {
      for (const line of this.splitter.push(chunk)) {
        if (!line.trim()) continue;
        let msg: WireOp;
        try {
          msg = JSON.parse(line) as WireOp;
        } catch {
          continue;
        }
        this.onFrame(msg);
      }
    });
    socket.on("close", () => {
      this.connected = false;
    });
    socket.on("error", () => {
      this.connected = false;
    });
  }

  async close(): Promise<void> {
    if (!this.connected) return;
    this.connected = false;
    const socket = this.socket;
    this.socket = null;
    this.handlers.clear();
    if (socket) {
      await new Promise<void>((resolve) => {
        socket.end(() => resolve());
        socket.destroy();
      });
    }
  }

  private onFrame(msg: WireOp): void {
    const op = msg.op;
    if (op === "msg") {
      const subId = msg["sub_id"] as string | undefined;
      const frame = msg["frame"] as string | undefined;
      if (!subId || frame === undefined) return;
      const handler = this.handlers.get(subId);
      if (!handler) return;
      let signal: Signal;
      try {
        signal = decode(frame);
      } catch {
        return;
      }
      void Promise.resolve(handler(signal)).catch(() => {
        /* handler errors are isolated */
      });
    }
  }

  private send(payload: WireOp): Promise<void> {
    if (!this.connected || this.socket === null) {
      return Promise.reject(new Error("DevSynapse not connected"));
    }
    const line = JSON.stringify(payload) + "\n";
    return new Promise<void>((resolve, reject) => {
      this.socket!.write(line, (err) => (err ? reject(err) : resolve()));
    });
  }

  /** @internal — used by DevSubscription. */
  async _sendUnsub(subId: string): Promise<void> {
    this.handlers.delete(subId);
    if (this.connected) {
      try {
        await this.send({ op: "unsub", sub_id: subId });
      } catch {
        /* socket may already be gone */
      }
    }
  }

  async publish(subject: string, signal: Signal): Promise<void> {
    const frame = Buffer.from(encode(signal)).toString("utf-8");
    await this.send({ op: "pub", subject, frame });
  }

  async subscribe(
    subject: string,
    handler: MessageHandler,
    opts: SubscribeOptions = {},
  ): Promise<Subscription> {
    this.subCounter += 1;
    const subId = `s${this.subCounter}-${Date.now().toString(36)}`;
    this.handlers.set(subId, handler);
    await this.send({
      op: "sub",
      sub_id: subId,
      subject,
      queue_group: opts.queueGroup ?? null,
    });
    return new DevSubscription(this, subId);
  }

  async request(
    subject: string,
    signal: Signal,
    opts: RequestOptions = {},
  ): Promise<Signal> {
    const timeoutMs = opts.timeoutMs ?? 5000;
    const replySubject = `_inbox.${signal.id}`;

    let settled = false;
    let resolveFn!: (s: Signal) => void;
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
      const enriched: Signal = {
        ...signal,
        meta: { ...signal.meta, _reply_to: replySubject },
      };
      await this.publish(subject, enriched);
      return await new Promise<Signal>((resolve, reject) => {
        const timer = setTimeout(() => {
          if (!settled) {
            settled = true;
            reject(new Error(`DevSynapse: no reply on '${replySubject}' within ${timeoutMs}ms`));
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

  /** Send `reply` to the `_reply_to` subject stored in `original.meta`. */
  async replyTo(original: Signal, reply: Signal): Promise<void> {
    const replySubject = original.meta["_reply_to"];
    if (typeof replySubject !== "string" || !replySubject) {
      throw new Error("Signal has no _reply_to in meta — not a request signal");
    }
    await this.publish(replySubject, reply);
  }
}
