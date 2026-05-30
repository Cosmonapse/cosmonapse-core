/**
 * @cosmonapse/sdk — unified Neuron factory
 *
 * Mirrors the Python `Neuron(source=...)` ergonomics in TypeScript: pick a
 * source, get back a NeuronFn you hand straight to an Axon. A Neuron is any
 * unit of real-world behaviour — an API (Express app) or an MCP server today,
 * with room for more sources later.
 *
 * ```ts
 * import { Axon, neuron } from "@cosmonapse/sdk";
 *
 * new Axon({ neuronId: "api",   neuronFn: neuron("express", { app }) });
 * new Axon({ neuronId: "files", neuronFn: neuron("mcp", { server: "filesystem", args: ["/data"] }) });
 * ```
 *
 * WHICH TO USE — `neuron(source, opts)` is the recommended, source-agnostic
 * entry point and the one mirrored from the Python SDK; prefer it in app code.
 * The standalone `expressNeuron(...)` / `mcpNeuron(...)` exports are the
 * lower-level primitives `neuron()` delegates to: reach for them only when you
 * want a single source's exact option type without the union, or to tree-shake
 * away the other source. They are not a second, parallel API — same behaviour,
 * narrower surface.
 */

import { expressNeuron, type CloseableNeuronFn, type ExpressNeuronOptions } from "./neuron-express.js";
import { mcpNeuron, type McpNeuronOptions } from "./neuron-mcp.js";

export type NeuronSource = "express" | "http" | "api" | "mcp";

export interface ExpressSourceOptions extends ExpressNeuronOptions {
  /** The Express app / Node request listener to serve in-process. */
  app: unknown;
}

export function neuron(source: "express" | "http" | "api", opts: ExpressSourceOptions): CloseableNeuronFn;
export function neuron(source: "mcp", opts: McpNeuronOptions): CloseableNeuronFn;
export function neuron(source: NeuronSource, opts: ExpressSourceOptions | McpNeuronOptions): CloseableNeuronFn {
  switch (source) {
    case "express":
    case "http":
    case "api": {
      const o = opts as ExpressSourceOptions;
      const { app, ...rest } = o;
      return expressNeuron(app, rest);
    }
    case "mcp":
      return mcpNeuron(opts as McpNeuronOptions);
    default: {
      const available = `"express", "http", "api", "mcp"`;
      throw new Error(`Unknown neuron source '${String(source)}'. Available: ${available}`);
    }
  }
}
