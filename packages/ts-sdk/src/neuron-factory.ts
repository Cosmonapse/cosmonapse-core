/**
 * @cosmonapse/sdk  -  unified Neuron factory
 *
 * Mirrors the Python `Neuron(source=...)` ergonomics in TypeScript: pick a
 * source, get back a NeuronFn you hand straight to an Axon. A Neuron is any
 * unit of real-world behaviour  -  an MCP server or an LLM provider today, with
 * room for more sources later.
 *
 * ```ts
 * import { Axon, neuron } from "@cosmonapse/sdk";
 *
 * new Axon({ neuronId: "files", neuronFn: neuron("mcp", { server: "filesystem", args: ["/data"] }) });
 * new Axon({ neuronId: "chat",  neuronFn: neuron("ollama", { model: "llama3" }) });
 * new Axon({ neuronId: "gpt",   neuronFn: neuron("openai", { model: "gpt-4o-mini" }) });
 * ```
 *
 * NOTE  -  there is no "express" / "http" / "api" source. An HTTP API is not a
 * Neuron: instead of wrapping a web app behind an Axon, keep your framework
 * (Express, Fastify, …) on the outside as an HTTP boundary and dispatch TASK
 * Signals from inside its route handlers via an orchestrator Dendrite. See the
 * `real-world-neurons` example.
 *
 * WHICH TO USE  -  `neuron(source, opts)` is the recommended, source-agnostic
 * entry point and the one mirrored from the Python SDK; prefer it in app code.
 * The standalone `mcpNeuron(...)` / `ollamaNeuron(...)` exports are the
 * lower-level primitives `neuron()` delegates to: reach for them only when you
 * want a single source's exact option type without the union, or to tree-shake
 * away the others. They are not a second, parallel API  -  same behaviour,
 * narrower surface.
 */

import { mcpNeuron, type McpNeuronOptions } from "./neuron-mcp.js";
import {
  huggingFaceNeuron,
  ollamaNeuron,
  type HuggingFaceNeuronOptions,
  type OllamaNeuronOptions,
} from "./neuron-http.js";
import {
  anthropicNeuron,
  openaiNeuron,
  type AnthropicNeuronOptions,
  type OpenAINeuronOptions,
} from "./neuron-openai.js";
import type { CloseableNeuronFn, NeuronFn } from "./neuron.js";

/**
 * Options for the OpenAI-compatible hosted aliases (`groq` / `openrouter` /
 * `together` / `mistral`). These are pre-configured {@link huggingFaceNeuron}s:
 * `endpoint` defaults to the provider's base URL and `apiKey` falls back to the
 * provider's env var, but both (and any other HuggingFace option) can be
 * overridden.
 */
export type OpenAICompatNeuronOptions = Omit<HuggingFaceNeuronOptions, "endpoint"> & {
  endpoint?: string;
};

type OpenAICompatAlias = "groq" | "openrouter" | "together" | "mistral";

const OPENAI_COMPAT: Record<OpenAICompatAlias, { endpoint: string; apiKeyEnv: string }> = {
  groq: { endpoint: "https://api.groq.com/openai", apiKeyEnv: "GROQ_API_KEY" },
  openrouter: { endpoint: "https://openrouter.ai/api", apiKeyEnv: "OPENROUTER_API_KEY" },
  together: { endpoint: "https://api.together.xyz", apiKeyEnv: "TOGETHER_API_KEY" },
  mistral: { endpoint: "https://api.mistral.ai", apiKeyEnv: "MISTRAL_API_KEY" },
};

/** Build a HuggingFace (OpenAI-compatible) neuron pre-pointed at a hosted provider. */
function openAICompatNeuron(alias: OpenAICompatAlias, opts: OpenAICompatNeuronOptions): NeuronFn {
  const { endpoint, apiKeyEnv } = OPENAI_COMPAT[alias];
  const apiKey = opts.apiKey ?? process.env[apiKeyEnv];
  const hfOpts: HuggingFaceNeuronOptions = {
    endpoint: opts.endpoint ?? endpoint,
    useChatApi: opts.useChatApi ?? true,
  };
  if (opts.model !== undefined) hfOpts.model = opts.model;
  if (opts.temperature !== undefined) hfOpts.temperature = opts.temperature;
  if (opts.maxNewTokens !== undefined) hfOpts.maxNewTokens = opts.maxNewTokens;
  if (opts.timeoutMs !== undefined) hfOpts.timeoutMs = opts.timeoutMs;
  if (apiKey !== undefined) hfOpts.apiKey = apiKey;
  return huggingFaceNeuron(hfOpts);
}

export type NeuronSource =
  | "mcp"
  | "ollama"
  | "huggingface"
  | "hf"
  | "openai"
  | "anthropic"
  | OpenAICompatAlias;

export function neuron(source: "mcp", opts: McpNeuronOptions): CloseableNeuronFn;
export function neuron(source: "ollama", opts: OllamaNeuronOptions): NeuronFn;
export function neuron(source: "huggingface" | "hf", opts: HuggingFaceNeuronOptions): NeuronFn;
export function neuron(source: "openai", opts: OpenAINeuronOptions): NeuronFn;
export function neuron(source: "anthropic", opts: AnthropicNeuronOptions): NeuronFn;
export function neuron(source: OpenAICompatAlias, opts?: OpenAICompatNeuronOptions): NeuronFn;
export function neuron(
  source: NeuronSource,
  opts?:
    | McpNeuronOptions
    | OllamaNeuronOptions
    | HuggingFaceNeuronOptions
    | OpenAINeuronOptions
    | AnthropicNeuronOptions
    | OpenAICompatNeuronOptions,
): CloseableNeuronFn | NeuronFn {
  switch (source) {
    case "mcp":
      return mcpNeuron(opts as McpNeuronOptions);
    case "ollama":
      return ollamaNeuron(opts as OllamaNeuronOptions);
    case "huggingface":
    case "hf":
      return huggingFaceNeuron(opts as HuggingFaceNeuronOptions);
    case "openai":
      return openaiNeuron(opts as OpenAINeuronOptions);
    case "anthropic":
      return anthropicNeuron(opts as AnthropicNeuronOptions);
    case "groq":
    case "openrouter":
    case "together":
    case "mistral":
      return openAICompatNeuron(source, (opts ?? {}) as OpenAICompatNeuronOptions);
    default: {
      const available =
        `"mcp", "ollama", "huggingface", "hf", "openai", "anthropic", ` +
        `"groq", "openrouter", "together", "mistral"`;
      throw new Error(`Unknown neuron source '${String(source)}'. Available: ${available}`);
    }
  }
}
