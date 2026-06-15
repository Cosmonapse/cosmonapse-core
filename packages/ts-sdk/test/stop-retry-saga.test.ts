import assert from "node:assert/strict";
import { test } from "node:test";

import {
  Axon,
  Dendrite,
  DevSynapse,
  DevSynapseServer,
  InMemoryEngram,
  MemorySynapse,
  PathwayClosedError,
  SignalType,
  defaultRetryOn,
  errorSignal,
  finalSignal,
  stopSignal,
  stoppedSignal,
  type Json,
  type Signal,
} from "../src/index.js";

const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms));

// --- unit: retry predicate + signal payloads -------------------------------

test("defaultRetryOn predicate", () => {
  const to = new Error("timed out");
  to.name = "TimeoutError";
  assert.equal(defaultRetryOn(to), true);
  assert.equal(defaultRetryOn(new PathwayClosedError("x")), true);
  assert.equal(
    defaultRetryOn(errorSignal({ traceId: "trc_a", parentId: null, code: "E", message: "m", recoverable: true })),
    true,
  );
  assert.equal(
    defaultRetryOn(errorSignal({ traceId: "trc_a", parentId: null, code: "E", message: "m", recoverable: false })),
    false,
  );
  assert.equal(defaultRetryOn(finalSignal({ traceId: "trc_a", parentId: "evt_test", result: { x: 1 } })), false);
});

test("stop/stopped signal payloads", () => {
  const s = stopSignal({ traceId: "trc_a", rollback: true, reason: "why" });
  assert.equal(s.type, SignalType.STOP);
  assert.deepEqual(s.payload, { rollback: true, reason: "why" });
  const a = stoppedSignal({ traceId: "trc_a", parentId: null, node: "ns", rolledBack: true, cancelled: 2, compensated: 3 });
  assert.equal(a.type, SignalType.STOPPED);
  assert.equal(a.payload["cancelled"], 2);
  assert.equal(a.payload["compensated"], 3);
  assert.equal(a.payload["rolled_back"], true);
});

// --- unit: engram saga journal ---------------------------------------------

test("saga add then compensate removes", async () => {
  const eng = new InMemoryEngram({ engramId: "e" });
  await eng.imprint("add", { id: "x1", content: "v" }, { traceId: "trc_1" });
  assert.ok(eng.snapshot().some((e) => e["id"] === "x1"));
  const n = await eng.compensate("trc_1");
  assert.equal(n, 1);
  assert.ok(!eng.snapshot().some((e) => e["id"] === "x1"));
});

test("saga upsert then compensate restores prior", async () => {
  const eng = new InMemoryEngram({ engramId: "e" });
  await eng.imprint("upsert", { content: "v1" }, { mergeKey: "k" }); // committed baseline
  await eng.imprint("upsert", { content: "v2" }, { mergeKey: "k", traceId: "trc_2" });
  assert.deepEqual(eng.snapshot().map((e) => e["content"]), ["v2"]);
  await eng.compensate("trc_2");
  assert.deepEqual(eng.snapshot().map((e) => e["content"]), ["v1"]);
});

test("saga delete then compensate restores", async () => {
  const eng = new InMemoryEngram({ engramId: "e" });
  await eng.imprint("add", { id: "d1", content: "keep" });
  await eng.imprint("delete", { id: "d1" }, { traceId: "trc_3" });
  assert.ok(!eng.snapshot().some((e) => e["id"] === "d1"));
  await eng.compensate("trc_3");
  assert.ok(eng.snapshot().some((e) => e["id"] === "d1" && e["content"] === "keep"));
});

test("saga commit discards journal", async () => {
  const eng = new InMemoryEngram({ engramId: "e" });
  await eng.imprint("add", { id: "c1", content: "v" }, { traceId: "trc_4" });
  await eng.commit("trc_4");
  const n = await eng.compensate("trc_4");
  assert.equal(n, 0);
  assert.ok(eng.snapshot().some((e) => e["id"] === "c1"));
});

// --- integration over a real async bus (DevSynapse/TCP) --------------------
// MemorySynapse delivers synchronously (publish awaits the handler), so a
// dispatch there blocks until the neuron finishes - mid-flight STOP and
// per-attempt timeouts can't be exercised. DevSynapse is the realistic async
// transport (matches production NATS/Kafka), so the control-flow tests run on it.

async function devBus(): Promise<{
  server: DevSynapseServer;
  workerSyn: DevSynapse;
  orchSyn: DevSynapse;
}> {
  const server = new DevSynapseServer({ host: "127.0.0.1", port: 0 });
  await server.start();
  const workerSyn = new DevSynapse({ url: server.url });
  const orchSyn = new DevSynapse({ url: server.url });
  await workerSyn.connect();
  await orchSyn.connect();
  return { server, workerSyn, orchSyn };
}

test("STOP abandons an in-flight neuron and the worker acks", async () => {
  const { server, workerSyn, orchSyn } = await devBus();
  const worker = new Dendrite({ synapse: workerSyn, namespace: "t", heartbeatMs: 0 });
  const orch = new Dendrite({ synapse: orchSyn, namespace: "t", dendriteId: "orch", heartbeatMs: 0 });

  let started = false;
  let completed = false;
  const slow = async (): Promise<Json> => {
    started = true;
    await sleep(3000);
    completed = true;
    return { done: true };
  };
  worker.attachAxon(new Axon({ neuronId: "slow", neuronFn: slow, capabilities: ["x"] }));

  const outputs: Signal[] = [];
  orch.onAgentOutput((s) => void outputs.push(s));

  await worker.start();
  await orch.start();
  try {
    const pw = await orch.dispatch({ neuron: "slow", input: {} });
    for (let i = 0; i < 400 && !started; i++) await sleep(5);
    assert.equal(started, true);
    const acks = await orch.stopTrace(pw.traceId, { collectAcks: true, timeoutMs: 400 });
    await sleep(30);
    assert.equal(completed, false);
    assert.equal(pw.closed, true);
    assert.equal(outputs.length, 0);
    assert.ok(
      acks.some((a) => Number(a.payload["cancelled"] ?? 0) >= 1),
      `no worker STOPPED ack with cancelled>=1: ${JSON.stringify(acks.map((a) => a.payload))}`,
    );
  } finally {
    await worker.stop();
    await orch.stop();
    await workerSyn.close();
    await orchSyn.close();
    await server.stop();
  }
});

test("runWithRetry succeeds after a stalled attempt", async () => {
  const { server, workerSyn, orchSyn } = await devBus();
  const worker = new Dendrite({ synapse: workerSyn, namespace: "t", heartbeatMs: 0 });
  const orch = new Dendrite({ synapse: orchSyn, namespace: "t", dendriteId: "orch", heartbeatMs: 0 });

  let calls = 0;
  const flaky = async (): Promise<Json> => {
    calls += 1;
    if (calls === 1) {
      await sleep(3000);
      return { late: true };
    }
    return { ok: calls };
  };
  worker.attachAxon(new Axon({ neuronId: "flaky", neuronFn: flaky, capabilities: ["x"] }));

  await worker.start();
  await orch.start();
  try {
    const sig = await orch.runWithRetry({
      neuron: "flaky",
      input: {},
      retry: { maxAttempts: 3, timeoutMs: 400 },
    });
    assert.equal(sig.type, SignalType.AGENT_OUTPUT);
    assert.deepEqual(sig.payload["output"], { ok: 2 });
    assert.equal(calls, 2);
  } finally {
    await worker.stop();
    await orch.stop();
    await workerSyn.close();
    await orchSyn.close();
    await server.stop();
  }
});

test("runWithRetry exhausts and throws TimeoutError", async () => {
  const { server, workerSyn, orchSyn } = await devBus();
  const worker = new Dendrite({ synapse: workerSyn, namespace: "t", heartbeatMs: 0 });
  const orch = new Dendrite({ synapse: orchSyn, namespace: "t", dendriteId: "orch", heartbeatMs: 0 });

  const hang = async (): Promise<Json> => {
    await sleep(5000);
    return {};
  };
  worker.attachAxon(new Axon({ neuronId: "hang", neuronFn: hang, capabilities: ["x"] }));

  await worker.start();
  await orch.start();
  try {
    let threw: Error | null = null;
    try {
      await orch.runWithRetry({ neuron: "hang", input: {}, retry: { maxAttempts: 2, timeoutMs: 250 } });
    } catch (err) {
      threw = err as Error;
    }
    assert.ok(threw !== null);
    assert.equal(threw?.name, "TimeoutError");
  } finally {
    await worker.stop();
    await orch.stop();
    await workerSyn.close();
    await orchSyn.close();
    await server.stop();
  }
});
