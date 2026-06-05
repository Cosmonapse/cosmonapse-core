/**
 * examples/orchestrator_api/express_app.ts
 * ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
 * Express integration: one module-level Dendrite, shared across all requests.
 * The Dendrite connects on startup and stays alive for the process lifetime.
 *
 * Run:
 *
 *   cosmo synapse start memory --namespace=api-demo   # terminal 1
 *   python examples/orchestrator_api/worker.py         # terminal 2
 *   npx tsx examples/orchestrator_api/express_app.ts   # terminal 3
 *
 *   curl -X POST http://localhost:3000/ask \
 *        -H "Content-Type: application/json" \
 *        -d '{"prompt": "What is a Synapse?"}'
 */

import express, { Request, Response } from "express";
import { Dendrite, NatsSynapse, newTraceId } from "@cosmonapse/sdk";

const SYNAPSE_URL = process.env.SYNAPSE_URL ?? "nats://127.0.0.1:4222";
const NAMESPACE   = "api-demo";
const PORT        = Number(process.env.PORT ?? 3000);

// ---------------------------------------------------------------------------
// Module-level Dendrite  -  connected once, reused for every request.
// ---------------------------------------------------------------------------
const synapse  = new NatsSynapse({ url: SYNAPSE_URL });
const dendrite = new Dendrite({
  synapse,
  namespace: NAMESPACE,
  dendriteId: "express-orchestrator",
  heartbeatMs: 0,
});

// Pending reply map: trace_id → resolve function.
const pending = new Map<string, (output: unknown) => void>();

dendrite.onAgentOutput((sig) => {
  const resolve = pending.get(sig.trace_id);
  if (resolve) {
    pending.delete(sig.trace_id);
    resolve((sig.payload as any).output);
  }
});

/**
 * Dispatch a TASK to the named neuron and wait for AGENT_OUTPUT.
 */
async function dispatch(
  neuron: string,
  input: Record<string, unknown>,
  timeoutMs = 30_000,
): Promise<unknown> {
  const traceId = newTraceId();
  const done    = new Promise<unknown>((resolve, reject) => {
    pending.set(traceId, resolve);
    setTimeout(() => {
      if (pending.delete(traceId)) {
        reject(new Error("worker timed out"));
      }
    }, timeoutMs);
  });
  await dendrite.dispatchTask({ neuron, input, traceId });
  return done;
}

// ---------------------------------------------------------------------------
// Express app
// ---------------------------------------------------------------------------
const app = express();
app.use(express.json());

app.post("/ask", async (req: Request, res: Response) => {
  const { prompt } = req.body as { prompt?: string };
  if (!prompt) {
    res.status(400).json({ error: "prompt is required" });
    return;
  }
  try {
    const output = await dispatch("worker", { prompt }) as any;
    res.json({ response: output?.response ?? "" });
  } catch (err: any) {
    res.status(504).json({ error: err.message });
  }
});

app.get("/health", (_req: Request, res: Response) => {
  res.json({ status: "ok" });
});

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------
async function main() {
  await synapse.connect();
  await dendrite.start();
  app.listen(PORT, () => {
    console.log(`express-orchestrator listening on http://localhost:${PORT}`);
  });
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
