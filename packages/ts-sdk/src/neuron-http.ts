/**
 * @cosmonapse/sdk  -  provider-backed Neurons (LLM over HTTP)
 *
 * Ported from `cosmonapse.neuron` (`_OllamaNeuron` / `_HuggingFaceNeuron`) and
 * `cosmonapse._neuron_base`. Wrap a running LLM server behind the `NeuronFn`
 * signature so it slots straight into an Axon.
 *
 * Uses the global `fetch` (Node 18+), so there is no extra dependency  -  the
 * Python port needs `httpx`; in Node the runtime ships the client.
 *
 * Input convention (shared with the Python SDK):
 *   - `prompt` (string)   -  single-turn input, or
 *   - `messages` (OpenAI-style `[{ role, content }]`)  -  multi-turn / system.
 *   (`text` / `query` / `content` are also accepted as a prompt alias.)
 *
 * Output: `{ response: "<text>", meta: <raw provider payload> }`.
 */

import type { Json } from "./envelope.js";
import type { NeuronFn } from "./neuron.js";

type Dict = Record<string, unknown>;

/** Pull a plain-text prompt from the common input keys. */
function readPrompt(input: Dict): string | null {
  const v = input["prompt"] ?? input["text"] ?? input["query"] ?? input["content"];
  return typeof v === "string" && v ? v : null;
}

/** Pull OpenAI-style messages if present. */
function readMessages(input: Dict): Array<Dict> | null {
  const m = input["messages"];
  return Array.isArray(m) ? (m as Dict[]) : null;
}

export function requireInput(input: Dict, provider: string): { prompt: string | null; messages: Dict[] | null } {
  const prompt = readPrompt(input);
  const messages = readMessages(input);
  if (!prompt && !messages) {
    throw new Error(
      `${provider} Neuron expects 'prompt' or 'messages' in the input dict. ` +
        `Got keys: ${Object.keys(input).join(", ")}`,
    );
  }
  return { prompt, messages };
}

export async function postJson(url: string, body: Dict, headers: Record<string, string>, timeoutMs: number): Promise<Json> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...headers },
      body: JSON.stringify(body),
      signal: ctrl.signal,
    });
    if (!res.ok) {
      const text = await res.text().catch(() => "");
      throw new Error(`HTTP ${res.status} from ${url}: ${text.slice(0, 200)}`);
    }
    return (await res.json()) as Json;
  } finally {
    clearTimeout(timer);
  }
}

// ---------------------------------------------------------------------------
// Ollama
// ---------------------------------------------------------------------------

export interface OllamaNeuronOptions {
  /** Ollama model tag, e.g. "llama3", "mistral", "phi3". */
  model: string;
  /** Base URL of the Ollama daemon. Default "http://localhost:11434". */
  endpoint?: string;
  /** Optional system prompt injected before any user message. */
  system?: string;
  temperature?: number;
  /** Maximum tokens to generate (`num_predict` in Ollama). */
  maxTokens?: number;
  /** HTTP timeout in ms. Default 120_000. */
  timeoutMs?: number;
}

/** Wrap a running Ollama daemon as a NeuronFn. */
export function ollamaNeuron(opts: OllamaNeuronOptions): NeuronFn {
  const endpoint = (opts.endpoint ?? "http://localhost:11434").replace(/\/+$/, "");
  const timeoutMs = opts.timeoutMs ?? 120_000;

  const options = (): Dict => {
    const o: Dict = {};
    if (opts.temperature !== undefined) o["temperature"] = opts.temperature;
    if (opts.maxTokens !== undefined) o["num_predict"] = opts.maxTokens;
    return o;
  };

  return async (input: Json): Promise<Json> => {
    const inp = (input ?? {}) as Dict;
    const { prompt, messages } = requireInput(inp, "Ollama");
    const opt = options();

    if (messages !== null) {
      const all = opts.system
        ? [{ role: "system", content: opts.system }, ...messages]
        : messages;
      const body: Dict = { model: opts.model, messages: all, stream: false };
      if (Object.keys(opt).length) body["options"] = opt;
      const data = (await postJson(`${endpoint}/api/chat`, body, {}, timeoutMs)) as Dict;
      const message = (data["message"] as Dict | undefined) ?? {};
      return { response: (message["content"] as string) ?? "", meta: data as Json };
    }

    const body: Dict = { model: opts.model, prompt: prompt ?? "", stream: false };
    if (opts.system) body["system"] = opts.system;
    if (Object.keys(opt).length) body["options"] = opt;
    const data = (await postJson(`${endpoint}/api/generate`, body, {}, timeoutMs)) as Dict;
    return { response: (data["response"] as string) ?? "", meta: data as Json };
  };
}

// ---------------------------------------------------------------------------
// HuggingFace TGI / vLLM / LM Studio / llama.cpp (OpenAI-compatible)
// ---------------------------------------------------------------------------

export interface HuggingFaceNeuronOptions {
  /** Base URL of the inference server, e.g. "http://localhost:8080". */
  endpoint: string;
  /** Model name forwarded in the chat-completions body (required for vLLM). */
  model?: string;
  /** Force the `/v1/chat/completions` path even for plain prompts. */
  useChatApi?: boolean;
  temperature?: number;
  /** Maximum tokens to generate. Default 512. */
  maxNewTokens?: number;
  /** Bearer token  -  use your HF Hub token for hosted endpoints. */
  apiKey?: string;
  /** HTTP timeout in ms. Default 120_000. */
  timeoutMs?: number;
}

/** Wrap a HuggingFace TGI endpoint (or any OpenAI-compatible server) as a NeuronFn. */
export function huggingFaceNeuron(opts: HuggingFaceNeuronOptions): NeuronFn {
  const endpoint = opts.endpoint.replace(/\/+$/, "");
  const maxNewTokens = opts.maxNewTokens ?? 512;
  const timeoutMs = opts.timeoutMs ?? 120_000;
  const headers: Record<string, string> = {};
  if (opts.apiKey) headers["Authorization"] = `Bearer ${opts.apiKey}`;

  return async (input: Json): Promise<Json> => {
    const inp = (input ?? {}) as Dict;
    const { prompt, messages } = requireInput(inp, "HuggingFace");

    if (messages !== null || opts.useChatApi) {
      const msgs = messages ?? [{ role: "user", content: prompt ?? "" }];
      const body: Dict = { messages: msgs, max_tokens: maxNewTokens };
      if (opts.model) body["model"] = opts.model;
      if (opts.temperature !== undefined) body["temperature"] = opts.temperature;
      const data = (await postJson(`${endpoint}/v1/chat/completions`, body, headers, timeoutMs)) as Dict;
      const choices = (data["choices"] as Dict[] | undefined) ?? [];
      const message = (choices[0]?.["message"] as Dict | undefined) ?? {};
      return { response: (message["content"] as string) ?? "", meta: data as Json };
    }

    const params: Dict = { max_new_tokens: maxNewTokens };
    if (opts.temperature !== undefined) params["temperature"] = opts.temperature;
    const body: Dict = { inputs: prompt ?? "", parameters: params };
    const data = await postJson(`${endpoint}/generate`, body, headers, timeoutMs);
    // TGI returns {"generated_text": "..."} or [{"generated_text": "..."}].
    let text = "";
    if (Array.isArray(data)) {
      text = (data[0] as Dict | undefined)?.["generated_text"] as string ?? "";
    } else {
      text = (data as Dict)["generated_text"] as string ?? "";
    }
    return { response: text, meta: data };
  };
}
