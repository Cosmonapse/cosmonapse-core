import assert from "node:assert/strict";
import { test } from "node:test";

import {
  Axon,
  Dendrite,
  MemoryRegistryStore,
  MemorySynapse,
} from "../src/index.js";

test("Dendrite mirrors its own attached Axons into the store on start", async () => {
  const syn = new MemorySynapse();
  await syn.connect();
  const store = new MemoryRegistryStore();
  const d = new Dendrite({ synapse: syn, registryStore: store, heartbeatMs: 0 });
  d.attachAxon(new Axon({ neuronId: "echo", neuronFn: () => ({}), capabilities: ["echo"] }));
  await d.start();

  const live = await d.findNeurons();
  assert.deepEqual(live.map((r) => r.neuron_id), ["echo"]);
  assert.deepEqual(live[0]!.capabilities, ["echo"]);

  await d.stop();
  await syn.close();
});

test("an orchestrator's store learns peers from REGISTER, loses them on DEREGISTER", async () => {
  const syn = new MemorySynapse();
  await syn.connect();

  const orchStore = new MemoryRegistryStore();
  const orchestrator = new Dendrite({
    synapse: syn,
    registryStore: orchStore,
    dendriteId: "orch",
    heartbeatMs: 0,
  });
  // Start the observer first so it is subscribed before the worker registers.
  await orchestrator.start();

  const worker = new Dendrite({ synapse: syn, heartbeatMs: 0 });
  worker.attachAxon(new Axon({ neuronId: "vision", neuronFn: () => ({}), capabilities: ["vision"] }));
  await worker.start();

  // Worker's REGISTER has propagated to the orchestrator's store.
  const seen = await orchestrator.findNeurons({ capability: "vision" });
  assert.deepEqual(seen.map((r) => r.neuron_id), ["vision"]);

  await worker.stop(); // emits DEREGISTER
  const liveAfter = await orchestrator.findNeurons();
  assert.equal(liveAfter.length, 0, "deregistered neuron is no longer live");
  const allAfter = await orchestrator.registrySnapshot({ includeDeregistered: true });
  assert.equal(allAfter[0]!.status, "deregistered");

  await orchestrator.stop();
  await syn.close();
});

test("registry helpers throw when no store is configured", async () => {
  const syn = new MemorySynapse();
  await syn.connect();
  const d = new Dendrite({ synapse: syn, heartbeatMs: 0 });
  await assert.rejects(() => d.findNeurons(), /no registryStore/);
  await syn.close();
});
