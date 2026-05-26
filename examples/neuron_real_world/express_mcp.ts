/**
 * examples/neuron_real_world/express_mcp.ts
 * ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
 * TypeScript counterpart: wrap an Express app and a standard MCP server as
 * Neurons. Both satisfy the same NeuronFn signature, so they attach to an Axon
 * identically — the protocol never knows what's behind them.
 *
 *   npm i express @modelcontextprotocol/sdk
 *   node --experimental-strip-types examples/neuron_real_world/express_mcp.ts
 */

import express from "express";
import {
  Axon,
  Dendrite,
  MemorySynapse,
  MemoryRegistryStore,
  expressNeuron,
  mcpNeuron,
  neuron, // unified factory, mirrors Python's Neuron(source=...)
} from "@cosmonapse/sdk";

// 1. An Express API → a Neuron
const app = express();
app.use(express.json());
app.post("/summarise", (req, res) => {
  const text: string = req.body?.text ?? "";
  res.json({ summary: text.slice(0, 120), length: text.length });
});

const apiNeuron = expressNeuron(app, { defaultPath: "/summarise" });
// equivalently: neuron("express", { app, defaultPath: "/summarise" });

// 2. The standard filesystem MCP server → a Neuron (wrapper only)
const fsNeuron = mcpNeuron({ server: "filesystem", args: ["."], tool: "list_directory" });
// equivalently: neuron("mcp", { server: "filesystem", args: ["."] });

async function main() {
  const synapse = new MemorySynapse();
  const store = new MemoryRegistryStore();

  const worker = new Dendrite({ synapse, namespace: "demo" });
  worker.attachAxon(new Axon({ neuronId: "summary-api", neuronFn: apiNeuron }));
  worker.attachAxon(new Axon({ neuronId: "files", neuronFn: fsNeuron }));

  const orch = new Dendrite({ synapse, registryStore: store, namespace: "demo" });

  orch.onAgentOutput(async (sig) => {
    console.log(sig.payload.neuron, "->", sig.payload.output);
    await orch.emitFinal({ traceId: sig.trace_id, parentId: sig.id, result: sig.payload.output });
  });

  await orch.start();
  await worker.start();
  try {
    await orch.dispatchTask({ neuron: "summary-api", input: { text: "anything can be a neuron" } });
    await orch.dispatchTask({ neuron: "files", input: { tool: "list_directory", arguments: { path: "." } } });
    await new Promise((r) => setTimeout(r, 2000));
  } finally {
    await worker.stop();
    await orch.stop();
    await apiNeuron.close();
    await fsNeuron.close();
  }
}

void main();
