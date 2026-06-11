/**
 * Tests for the Engram Dendrite/Axon wiring port: hosting (attachEngram +
 * RECALL/IMPRINT routing + engram REGISTER), caller side (dendrite.recall /
 * imprint over the EngramClient), the Axon binding whitelist + the
 * context-object helpers (the TS answer to Python's kwargs injection),
 * ambient-trace attribution, and terminal-event cancellation.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  Axon,
  Dendrite,
  EngramBinding,
  EngramNotBound,
  InMemoryEngram,
  MemorySynapse,
  SignalType,
  type Json,
  type NeuronHelpers,
  type Signal,
} from "../src/index.js";

async function bus(): Promise<MemorySynapse> {
  const syn = new MemorySynapse();
  await syn.connect();
  return syn;
}

function memEngram(id = "mem1", kind = "kv"): InMemoryEngram {
  return new InMemoryEngram({ engramId: id, engramKind: kind });
}

// ---------------------------------------------------------------------------
// Hosting + caller round trips
// ---------------------------------------------------------------------------

test("imprint then recall round-trips through a hosted Engram", async () => {
  const syn = await bus();
  const host = new Dendrite({ synapse: syn, dendriteId: "host", heartbeatMs: 0 });
  await host.attachEngram(memEngram());
  const caller = new Dendrite({ synapse: syn, dendriteId: "caller", heartbeatMs: 0 });
  await host.start();
  await caller.start();
  try {
    const receipt = await caller.imprint({
      engramId: "mem1",
      op: "add",
      entry: { content: "favorite color is blue", tags: ["fav"] },
      awaitAck: true,
      deadlineMs: 2000,
    });
    assert.ok(receipt && receipt.ok);
    const result = await caller.recall({
      engramId: "mem1",
      query: { text: "blue" },
      deadlineMs: 2000,
    });
    assert.equal(result.hits.length, 1);
    assert.match(String(result.hits[0]!.entry["content"]), /blue/);
    assert.deepEqual(result.engramIds, ["mem1"]);
  } finally {
    await caller.stop();
    await host.stop();
    await syn.close();
  }
});

test("engramKind addressing reaches the host; engram REGISTER is learned, not a neuron", async () => {
  const syn = await bus();
  const host = new Dendrite({ synapse: syn, dendriteId: "host", heartbeatMs: 0 });
  await host.attachEngram(memEngram("mem1", "kv"));
  const peer = new Dendrite({ synapse: syn, dendriteId: "peer", heartbeatMs: 0 });
  const neuronRegisters: Signal[] = [];
  peer.onRegister((s) => void neuronRegisters.push(s));
  await peer.start();
  await host.start(); // host announces its engram REGISTER after peer listens
  try {
    await new Promise((r) => setTimeout(r, 20));
    // Learned as an Engram, not surfaced to onRegister neuron handlers.
    assert.equal(peer.isEngramKnown({ engramKind: "kv" }), true);
    assert.equal(peer.isEngramKnown({ engramId: "mem1" }), true);
    assert.equal(neuronRegisters.length, 0);

    await peer.imprint({
      engramKind: "kv",
      op: "add",
      entry: { content: "note-k" },
      awaitAck: true,
      deadlineMs: 2000,
    });
    const res = await peer.recall({ engramKind: "kv", query: { text: "note-k" }, deadlineMs: 2000 });
    assert.equal(res.hits.length, 1);
  } finally {
    await peer.stop();
    await host.stop();
    await syn.close();
  }
});

test("recall deadline rejects when no Engram serves the address", async () => {
  const syn = await bus();
  const caller = new Dendrite({ synapse: syn, heartbeatMs: 0 });
  await caller.start();
  try {
    await assert.rejects(
      caller.recall({ engramId: "nobody", query: { text: "x" }, deadlineMs: 100 }),
      /deadline/,
    );
  } finally {
    await caller.stop();
    await syn.close();
  }
});

// ---------------------------------------------------------------------------
// Axon bindings + context-object helpers
// ---------------------------------------------------------------------------

test("Neuron uses helpers.recall/imprint via declared bindings", async () => {
  const syn = await bus();
  const host = new Dendrite({ synapse: syn, dendriteId: "host", heartbeatMs: 0 });
  await host.attachEngram(memEngram("prefs", "kv"));

  const worker = new Dendrite({ synapse: syn, role: "worker", heartbeatMs: 0 });
  worker.attachAxon(
    new Axon({
      neuronId: "remember",
      engrams: [new EngramBinding({ name: "prefs", directedId: "prefs" })],
      neuronFn: async (input: Json, _ctx: unknown[], helpers?: NeuronHelpers) => {
        assert.ok(helpers, "helpers must be passed when bindings are declared");
        if (input["save"] !== undefined) {
          await helpers.imprint("prefs", {
            op: "add",
            entry: { content: String(input["save"]), tags: ["fav"] },
            awaitAck: true,
            deadlineMs: 2000,
          });
          return { saved: true };
        }
        const res = (await helpers.recall("prefs", {
          query: { tag: "fav" },
          deadlineMs: 2000,
        })) as { hits: Array<{ entry: Json }> };
        return { fav: res.hits[0]?.entry["content"] ?? null };
      },
    }),
  );
  const orch = new Dendrite({ synapse: syn, heartbeatMs: 0 });
  await host.start();
  await worker.start();
  await orch.start();
  try {
    const saved = await orch.dispatchAndWait({
      neuron: "remember",
      input: { save: "teal" },
      timeoutMs: 3000,
    });
    assert.deepEqual(saved.payload["output"], { saved: true });
    const read = await orch.dispatchAndWait({ neuron: "remember", input: {}, timeoutMs: 3000 });
    assert.deepEqual(read.payload["output"], { fav: "teal" });
  } finally {
    await orch.stop();
    await worker.stop();
    await host.stop();
    await syn.close();
  }
});

test("undeclared binding name fails loudly as an ERROR signal", async () => {
  const syn = await bus();
  const worker = new Dendrite({ synapse: syn, role: "worker", heartbeatMs: 0 });
  worker.attachAxon(
    new Axon({
      neuronId: "loud",
      engrams: [new EngramBinding({ name: "prefs", directedId: "prefs" })],
      neuronFn: async (_i: Json, _c: unknown[], helpers?: NeuronHelpers) => {
        await helpers!.recall("not-a-binding", { query: {} });
        return {};
      },
    }),
  );
  const orch = new Dendrite({ synapse: syn, heartbeatMs: 0 });
  await worker.start();
  await orch.start();
  try {
    const err = await orch.dispatchAndWait({ neuron: "loud", input: {}, timeoutMs: 2000 });
    assert.equal(err.type, SignalType.ERROR);
    assert.match(String(err.payload["message"]), /no Engram binding named 'not-a-binding'/);
  } finally {
    await orch.stop();
    await worker.stop();
    await syn.close();
  }
});

test("duplicate binding names are rejected at construction", () => {
  assert.throws(
    () =>
      new Axon({
        neuronId: "dup",
        neuronFn: () => ({}),
        engrams: [
          new EngramBinding({ name: "a", directedId: "x" }),
          new EngramBinding({ name: "a", directedId: "y" }),
        ],
      }),
    /duplicate EngramBinding name/,
  );
});

// ---------------------------------------------------------------------------
// Ambient trace attribution
// ---------------------------------------------------------------------------

test("dendrite.imprint inside a task inherits the task's trace (ambient)", async () => {
  const syn = await bus();
  const host = new Dendrite({ synapse: syn, dendriteId: "host", heartbeatMs: 0 });
  await host.attachEngram(memEngram("log", "kv"));

  const worker = new Dendrite({ synapse: syn, role: "worker", heartbeatMs: 0 });
  worker.attachAxon(
    new Axon({
      neuronId: "logger",
      neuronFn: async () => {
        // No explicit trace ids: must inherit the ambient TASK trace.
        await worker.imprint({
          engramId: "log",
          op: "add",
          entry: { content: "hi" },
        });
        return { ok: true };
      },
    }),
  );
  const orch = new Dendrite({ synapse: syn, heartbeatMs: 0 });
  const imprints: Signal[] = [];
  orch.onImprintSignal((s) => void imprints.push(s));
  await host.start();
  await worker.start();
  await orch.start();
  try {
    const out = await orch.dispatchAndWait({ neuron: "logger", input: {}, timeoutMs: 2000 });
    assert.equal(out.type, SignalType.AGENT_OUTPUT);
    await new Promise((r) => setTimeout(r, 30));
    assert.equal(imprints.length, 1);
    assert.equal(imprints[0]!.trace_id, out.trace_id); // ambient attribution
  } finally {
    await orch.stop();
    await worker.stop();
    await host.stop();
    await syn.close();
  }
});

// ---------------------------------------------------------------------------
// detach + lifecycle
// ---------------------------------------------------------------------------

test("detachEngram stops serving; attachEngram while running serves immediately", async () => {
  const syn = await bus();
  const host = new Dendrite({ synapse: syn, dendriteId: "host", heartbeatMs: 0 });
  const caller = new Dendrite({ synapse: syn, heartbeatMs: 0 });
  await host.start();
  await caller.start();
  try {
    await host.attachEngram(memEngram("live", "kv")); // hot attach
    const r = await caller.imprint({
      engramId: "live",
      op: "add",
      entry: { content: "alive" },
      awaitAck: true,
      deadlineMs: 2000,
    });
    assert.ok(r && r.ok);
    await host.detachEngram("live");
    await assert.rejects(
      caller.recall({ engramId: "live", query: { text: "alive" }, deadlineMs: 100 }),
      /deadline/,
    );
  } finally {
    await caller.stop();
    await host.stop();
    await syn.close();
  }
});
