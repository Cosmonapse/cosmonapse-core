/**
 * @cosmonapse/sdk — Express / HTTP-app Neuron
 *
 * Turn an existing **Express app** (or any Node HTTP request listener) into a
 * Neuron: the Axon hands the TASK's `input` to the Neuron, the Neuron replays
 * it as an HTTP request against the app, and the response becomes the output.
 *
 * The app is mounted once on an ephemeral loopback port the first time the
 * Neuron is called, and reused for every subsequent TASK. Call `.close()`
 * (wired up automatically when the Axon deregisters) to shut the server down.
 *
 * ```ts
 * import express from "express";
 * import { Axon, expressNeuron } from "@cosmonapse/sdk";
 *
 * const app = express();
 * app.use(express.json());
 * app.post("/summarise", (req, res) => res.json({ summary: req.body.text.slice(0, 100) }));
 *
 * const axon = new Axon({ neuronId: "summary-api", neuronFn: expressNeuron(app) });
 * ```
 *
 * Input dict (all keys optional):
 *   method            HTTP method (default: `defaultMethod`, "POST").
 *   path | url        Request path (default: `defaultPath`, "/").
 *   json              JSON body (object/array).
 *   data              Raw body when `json` is absent (string, or object→JSON).
 *   query | params    Query-string params (object).
 *   headers           Extra request headers (object).
 *
 * Convenience: if no `json`/`data` and no explicit `path`/`url` are given but
 * the input carries `prompt`/`text`/`content`, the whole input (minus control
 * keys) is sent as the JSON body to `defaultPath` — so an HTTP neuron accepts
 * the same `{ prompt }` shape an LLM neuron does.
 *
 * Output:
 *   { status, ok, json, response, headers, meta:{method,path} }
 */

import http from "node:http";
import type { AddressInfo } from "node:net";
import type { Json } from "./envelope.js";
import type { NeuronFn } from "./neuron.js";

export interface ExpressNeuronOptions {
  /** Method used when input omits `method`. Default "POST". */
  defaultMethod?: string;
  /** Path used when input omits `path`/`url`. Default "/". */
  defaultPath?: string;
  /** Headers merged into every request. */
  baseHeaders?: Record<string, string>;
  /** Host to bind the in-process server to. Default "127.0.0.1". */
  host?: string;
}

/** A NeuronFn that also exposes an async `close()` to release the server. */
export interface CloseableNeuronFn extends NeuronFn {
  close(): Promise<void>;
}

const CONTROL_KEYS = new Set([
  "method",
  "path",
  "url",
  "json",
  "data",
  "query",
  "params",
  "headers",
]);

type RequestListener = (req: http.IncomingMessage, res: http.ServerResponse) => void;

export function expressNeuron(
  app: RequestListener | unknown,
  opts: ExpressNeuronOptions = {},
): CloseableNeuronFn {
  if (app == null) {
    throw new Error("expressNeuron(app) requires an Express app / request listener.");
  }

  const host = opts.host ?? "127.0.0.1";
  let server: http.Server | null = null;
  let baseUrl: string | null = null;
  let starting: Promise<string> | null = null;

  async function ready(): Promise<string> {
    if (baseUrl) return baseUrl;
    if (starting) return starting;
    starting = new Promise<string>((resolve, reject) => {
      const srv = http.createServer(app as RequestListener);
      srv.once("error", reject);
      srv.listen(0, host, () => {
        const addr = srv.address() as AddressInfo;
        server = srv;
        baseUrl = `http://${host}:${addr.port}`;
        resolve(baseUrl);
      });
    });
    return starting;
  }

  const fn = (async (input: Json, _context: unknown[]): Promise<Json> => {
    const base = await ready();
    const inp = (input ?? {}) as Record<string, unknown>;

    const method = String(inp.method ?? opts.defaultMethod ?? "POST").toUpperCase();
    const path = String(inp.path ?? inp.url ?? opts.defaultPath ?? "/");

    const headers: Record<string, string> = {
      ...(opts.baseHeaders ?? {}),
      ...((inp.headers as Record<string, string>) ?? {}),
    };

    let jsonBody = inp.json;
    const data = inp.data;
    const query = (inp.query ?? inp.params) as Record<string, unknown> | undefined;

    // prompt-convenience: input becomes the JSON body at the default path.
    const hasExplicitPath = inp.path != null || inp.url != null;
    const promptish = inp.prompt ?? inp.text ?? inp.content;
    if (jsonBody == null && data == null && !hasExplicitPath && promptish != null) {
      const body: Record<string, unknown> = {};
      for (const [k, v] of Object.entries(inp)) {
        if (!CONTROL_KEYS.has(k)) body[k] = v;
      }
      jsonBody = body;
    }

    const url = new URL(base + path);
    if (query && typeof query === "object") {
      for (const [k, v] of Object.entries(query)) url.searchParams.set(k, String(v));
    }

    let fetchBody: string | undefined;
    if (jsonBody != null) {
      if (!("content-type" in headers) && !("Content-Type" in headers)) {
        headers["content-type"] = "application/json";
      }
      fetchBody = JSON.stringify(jsonBody);
    } else if (data != null) {
      fetchBody = typeof data === "string" ? data : JSON.stringify(data);
    }

    const noBody = method === "GET" || method === "HEAD";
    const resp = await fetch(url, {
      method,
      headers,
      body: noBody ? undefined : fetchBody,
    });

    const text = await resp.text();
    const ctype = resp.headers.get("content-type") ?? "";
    let parsed: Json | null = null;
    if (ctype.includes("application/json") && text) {
      try {
        parsed = JSON.parse(text) as Json;
      } catch {
        parsed = null;
      }
    }

    const outHeaders: Record<string, string> = {};
    resp.headers.forEach((v, k) => {
      outHeaders[k] = v;
    });

    return {
      status: resp.status,
      ok: resp.status < 400,
      json: parsed,
      response: text,
      headers: outHeaders,
      meta: { method, path },
    } as Json;
  }) as CloseableNeuronFn;

  fn.close = async (): Promise<void> => {
    const srv = server;
    server = null;
    baseUrl = null;
    starting = null;
    if (srv) {
      await new Promise<void>((resolve) => srv.close(() => resolve()));
    }
  };

  return fn;
}
