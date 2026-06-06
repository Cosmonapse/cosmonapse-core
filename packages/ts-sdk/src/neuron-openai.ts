/**
 * @cosmonapse/sdk  -  hosted-LLM provider Neurons (OpenAI, Anthropic)
 *
 * Ported from `cosmonapse.neuron` (`_OpenAINeuron` / `_AnthropicNeuron`). Wrap a
 * hosted LLM API behind the `NeuronFn` signature so it slots straight into an
 * Axon. Uses the global `fetch` (Node 18+), so there is no extra dependency  -
 * the Python port needs `httpx`; in Node the runtime ships the client.
 *
 * Input convention (shared with the Python SDK):
 *   - `prompt` (string)  -  single-turn input, or
 *   - `messages` (OpenAI-style `[{ role, content }]`)  -  multi-turn / system.
 *   (`text` / `query` / `content` are also accepted as a prompt alias.)
 *
 * Output: `{ response: "<text>", meta: <raw provider payload> }`.
 */

import type { Json } from "./envelope.js";
import type { NeuronFn } from "./neuron.js";
import { postJson, requireInput } from "./neuron-http.js";

type Dict = Record<string, unknown>;

// ---------------------------------------------------------------------------
// OpenAI  (Chat Completions API)
// ---------------------------------------------------------------------------

export interface OpenAINeuronOptions {
  /** Chat model name, e.g. "gpt-4o", "gpt-4o-mini". */
  model: string;
  /** API key. If omitted, falls back to the `OPENAI_API_KEY` env var. */
  apiKey?: string;
  /** API base URL. Default "https://api.openai.com/v1" (point at Azure/proxies). */
  endpoint?: string;
  temperature?: number;
  /** Maximum tokens to generate. */
  maxTokens?: number;
  /** Optional system prompt injected as the first `system` message. */
  system?: string;
  /** HTTP timeout in ms. Default 120_000. */
  timeoutMs?: number;
}

/** Wrap the OpenAI Chat Completions API (or a compatible proxy) as a NeuronFn. */
export function openaiNeuron(opts: OpenAINeuronOptions): NeuronFn {
  const key = opts.apiKey ?? process.env["OPENAI_API_KEY"];
  if (!key) {
    throw new Error(
      "OpenAI Neuron requires an API key. Pass apiKey=... or set the " +
        "OPENAI_API_KEY environment variable.",
    );
  }
  const endpoint = (opts.endpoint ?? "https://api.openai.com/v1").replace(/\/+$/, "");
  const timeoutMs = opts.timeoutMs ?? 120_000;
  const headers: Record<string, string> = { Authorization: `Bearer ${key}` };

  return async (input: Json): Promise<Json> => {
    const inp = (input ?? {}) as Dict;
    const { prompt, messages } = requireInput(inp, "OpenAI");

    let msgs: Dict[] = messages !== null ? [...messages] : [{ role: "user", content: prompt ?? "" }];
    if (opts.system) msgs = [{ role: "system", content: opts.system }, ...msgs];

    const body: Dict = { model: opts.model, messages: msgs };
    if (opts.temperature !== undefined) body["temperature"] = opts.temperature;
    if (opts.maxTokens !== undefined) body["max_tokens"] = opts.maxTokens;

    const data = (await postJson(`${endpoint}/chat/completions`, body, headers, timeoutMs)) as Dict;
    const choices = (data["choices"] as Dict[] | undefined) ?? [];
    const message = (choices[0]?.["message"] as Dict | undefined) ?? {};
    return { response: (message["content"] as string) ?? "", meta: data as Json };
  };
}

// ---------------------------------------------------------------------------
// Anthropic  (Messages API)
// ---------------------------------------------------------------------------

const ANTHROPIC_ENDPOINT = "https://api.anthropic.com/v1";
const ANTHROPIC_VERSION = "2023-06-01";

export interface AnthropicNeuronOptions {
  /** Claude model name, e.g. "claude-opus-4-6", "claude-sonnet-4-6". */
  model: string;
  /** API key. If omitted, falls back to the `ANTHROPIC_API_KEY` env var. */
  apiKey?: string;
  /**
   * Optional system prompt. Sent as the top-level `system` field (the Anthropic
   * API does not accept a `system` role inside `messages`).
   */
  system?: string;
  /** Maximum tokens to generate. Required by the API; defaults to 1024. */
  maxTokens?: number;
  temperature?: number;
  /** HTTP timeout in ms. Default 120_000. */
  timeoutMs?: number;
}

/** Wrap the Anthropic Messages API as a NeuronFn. */
export function anthropicNeuron(opts: AnthropicNeuronOptions): NeuronFn {
  const key = opts.apiKey ?? process.env["ANTHROPIC_API_KEY"];
  if (!key) {
    throw new Error(
      "Anthropic Neuron requires an API key. Pass apiKey=... or set the " +
        "ANTHROPIC_API_KEY environment variable.",
    );
  }
  const maxTokens = opts.maxTokens ?? 1024;
  const timeoutMs = opts.timeoutMs ?? 120_000;
  const headers: Record<string, string> = {
    "anthropic-version": ANTHROPIC_VERSION,
    "x-api-key": key,
  };

  return async (input: Json): Promise<Json> => {
    const inp = (input ?? {}) as Dict;
    const { prompt, messages } = requireInput(inp, "Anthropic");

    // Anthropic takes `system` as a top-level field, not a message role. Pull
    // any system-role entries out of the message list and promote them.
    let system = opts.system;
    let msgs: Dict[];
    if (messages !== null) {
      const systemMsgs = messages.filter((m) => m["role"] === "system");
      if (systemMsgs.length > 1) {
        console.warn("Anthropic Neuron received multiple system messages; using the last one.");
      }
      const lastSystem = systemMsgs[systemMsgs.length - 1];
      if (lastSystem && typeof lastSystem["content"] === "string") {
        system = lastSystem["content"];
      }
      msgs = messages.filter((m) => m["role"] !== "system");
    } else {
      msgs = [{ role: "user", content: prompt ?? "" }];
    }

    const body: Dict = { model: opts.model, messages: msgs, max_tokens: maxTokens };
    if (system) body["system"] = system;
    if (opts.temperature !== undefined) body["temperature"] = opts.temperature;

    const data = (await postJson(`${ANTHROPIC_ENDPOINT}/messages`, body, headers, timeoutMs)) as Dict;
    const blocks = (data["content"] as Dict[] | undefined) ?? [];
    const text = blocks
      .filter((b) => b["type"] === "text")
      .map((b) => (b["text"] as string) ?? "")
      .join("");
    return { response: text, meta: data as Json };
  };
}
