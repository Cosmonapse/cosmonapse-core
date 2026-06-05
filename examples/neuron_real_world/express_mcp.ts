/**
 * examples/neuron_real_world/express_mcp.ts
 * ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
 * TypeScript counterpart. A Neuron is anything that interacts with the real
 * world  -  here, the standard filesystem MCP server and a plain function. But
 * an **HTTP API is not a Neuron**: instead of wrapping the Express app behind
 * an Axon, we keep Express on the *outside* as an HTTP boundary and dispatch
 * TASK Signals from inside its route handlers via an orchestrator Dendrite.
 *
 *   npm i express @modelcontextprotocol/sdk
 *   node --experimental-strip-types examples/neuron_real_world/express_mcp.ts
 *
 *   # then, in another terminal:
 *   curl -s -X POST localhost:5000/summarise \
 *        -H 'Content-Type: application/json' -d '{"text":"keep the API at the edge"}'
 *   curl -s -X POST localhost:5000/files
 */

import express from "express";
import {
  Axon,
  Dendrite,
  MemorySynapse,
  mcpNeuron,
  newTraceId,
  type Json,
  type NeuronFn,
} from "@cosmonapse/sdk";

const NS = "demo";
const PORT = 5000;

// 1. Real-world Neurons (the worker side)  -  neither knows about HTTP.
const summaryNeuron: NeuronFn = (input) => {
  const text = String((input as Record<string, unknown>)?.text ?? "");
  return { summary: text.slice(0, 120), length: text.length };
};

// The standard filesystem MCP server, wrapped as a Neuron (wrapper only).
const filesNeuron = mcpNeuron({ server: "filesystem", args: ["."], tool: "list_directory" });

async function main() {
  const synapse = new MemorySynapse();
  await synapse.connect();

  // Worker Dendrite: hosts the Axons and replies to TASKs.
  const worker = new Dendrite({ synapse, namespace: NS, dendriteId: "workers", heartbeatMs: 0 });
  worker.attachAxon(new Axon({ neuronId: "summary", neuronFn: summaryNeuron, capabilities: ["summarise"] }));
  worker.attachAxon(new Axon({ neuronId: "files", neuronFn: filesNeuron, capabilities: ["mcp", "filesystem"] }));
  await worker.start();

  // Orchestrator Dendrite: lives inside the web process. Its onAgentOutput
  // handler resolves the Promise the Express route is awaiting.
  const orch = new Dendrite({ synapse, namespace: NS, dendriteId: "http-edge", heartbeatMs: 0 });
  const pending = new Map<string, (out: Json) => void>();
  orch.onAgentOutput((sig) => {
    const resolve = pending.get(sig.trace_id);
    if (resolve) {
   