import assert from "node:assert/strict";
import { test } from "node:test";

import {
  Axon,
  clarify,
  Dendrite,
  MemorySynapse,
  type Json,
  type Signal,
} from "../src/index.js";

/** Stand up a worker Dendrite (hosts the axon) + an orchestrator on one bus. */
async function harness(neuronFn: (input: Json, ctx: unknown[]) => Json | Promise<Json>) {
  const syn = new MemorySynapse();
  await syn.connect();

  const axon = new Axon({ neuronId: "echo", neuronFn, capabilities: ["echo"] });
  const worker = new Dendrite({ synapse: syn, heartbeatMs: 0 });
  worker.attachAxon(axon);

  const orchestrator = new Dendrite({ synapse: syn, dendriteId: "orch", heartbeatMs: 0 });
  const received: Signal[] = [];
  orchestrator.onAgentOutput((s) => void received.push(s));
  orchestrator.onClarification((s) => void received.push(s));
  orchestrator.onErrorSignal((s) => void received.push(s));

  await worker.start();
  await orchestrator.start();
  return { syn, worker, orchestrator, received };
}

test("end-to-end: TASK -> Neuron -> Axon -> AGENT_OUTPUT", async () => {
  const { worker, orchestrator, received, syn } = await harness((input) => ({
    seen: input,
  }));

  const task = await orchestrator.dispatchTask({ neuron: "echo", input: { hi: "there" } });
  await new Promise((r) => setTimeout(r, 0));

  assert.equal(received.length, 1);
  const out = received[0]!;
  assert.equal(out.type, "AGENT_OUTPUT");
  assert.equal(out.directed?.id, "echo");
  assert.equal(out.trace_id, task.trace_id); // same workflow
  assert.equal(out.parent_id, task.id); // caused by the task
  assert.deepEqual(out.payload["output"], { seen: { hi: "there" } });

  await worker.stop();
  await orchestrator.stop();
  await syn.close();
});

test("clarification marker becomes a CLARIFICATION signal", async () => {
  const { worker, orchestrator, received, syn } = await harness(() =>
    clarify("which file?", { hint: "path" }),
  );
  await orchestrator.dispatchTask({ neuron: "echo", input: {} });
  await new Promise((r) => setTimeout(r, 0));

  assert.equal(received[0]!.type, "CLARIFICATION");
  assert.equal(received[0]!.payload["question"], "which file?");

  await worker.stop();
  await orchestrator.stop();
  await syn.close();
});

test("a thrown Neuron becomes an ERROR signal", async () => {
  const { worker, orchestrator, received, syn } = await harness(() => {
    throw new Error("boom");
  });
  await orchestrator.dispatchTask({ neuron: "echo", input: {} });
  await new Promise((r) => setTimeout(r, 0));

  assert.equal(received[0]!.type, "ERROR");
  assert.equal(received[0]!.payload["code"], "NEURON_EXCEPTION");
  assert.equal(received[0]!.payload["message"], "boom");

  await worker.stop();
  await orchestrator.stop();
  await syn.close();
});

test("Dendrite refuses to emit an Axon-owned type", async () => {
  const syn = new MemorySynapse();
  await syn.connect();
  const d = new Dendrite({ synapse: syn, heartbeatMs: 0 });
  const agentOut: Signal = {
    v: "1",
    id: "evt_x",
    trace_id: "trc_x",
    parent_id: null,
    type: "AGENT_OUTPUT",
    neuron: "n",
    ts: new Date().toISOString(),
    payload: {},
    meta: {},
  };
  await assert.rejects(() => d.emit(agentOut), /Axon-owned type/);
  await syn.close();
});

test("attaching two Axons with the same id throws", async () => {
  const syn = new MemorySynapse();
  await syn.connect();
  const d = new Dendrite({ synapse: syn, heartbeatMs: 0 });
  const fn = () => ({});
  d.attachAxon(new Axon({ neuronId: "dup", neuronFn: fn }));
  assert.throws(() => d.attachAxon(new Axon({ neuronId: "dup", neuronFn: fn })), /already has an Axon/);
  await syn.close();
});
