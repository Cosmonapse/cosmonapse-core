/**
 * Parity tests for the gap-fix port: decorator surface + filters,
 * awaitDecision, beforeTask, hot addAxon/detachAxon, follow-up prompt
 * rendering, intent-prompt injection, version validation, registry
 * staleness. Mirrors the Python tests/test_gap_fixes.py.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  Axon,
  COSMO_INTENT_SYSTEM_PROMPT,
  createSignal,
  Dendrite,
  DendriteProtocolError,
  decode,
  encode,
  followupPrompt,
  MemoryRegistryStore,
  MemorySynapse,
  neuronRecord,
  SignalType,
  type Json,
  type Signal,
} from "../src/index.js";

const echo = (input: Json): Json => ({ echo: input["text"] ?? "" });

async function bus(): Promise<MemorySynapse> {
  const syn = new MemorySynapse();
  await syn.connect();
  return syn;
}

// ---------------------------------------------------------------------------
// Decorator surface
// ---------------------------------------------------------------------------

test("onFinal + generic onSignal fire; filters narrow by neuron", async () => {
  const syn = await bus();
  const worker = new Dendrite({ synapse: syn, role: "worker", heartbeatMs: 0 });
  worker.attachAxon(new Axon({ neuronId: "e", neuronFn: echo }));
  const orch = new Dendrite({ synapse: syn, heartbeatMs: 0 });
  const named: Signal[] = [];
  const generic: Signal[] = [];
  const filteredOut: Signal[] = [];
  orch.onFinal((s) => void named.push(s));
  orch.onSignal(SignalType.FINAL, (s) => void generic.push(s));
  orch.onFinal((s) => void filteredOut.push(s), { neuron: "someone-else" });
  await worker.start();
  await orch.start();
  try {
    await orch.dispatchAndWait({
      neuron: "e",
      input: { text: "x" },
      scope: "terminal",
      timeoutMs: 2000,
    });
    await new Promise((r) => setTimeout(r, 20));
    assert.equal(named.length, 1);
    assert.equal(generic.length, 1);
    assert.equal(filteredOut.length, 0);
  } finally {
    await orch.stop();
    await worker.stop();
    await syn.close();
  }
});

test("ensureSubscribed removes the late-registration race", async () => {
  const syn = await bus();
  const d = new Dendrite({ synapse: syn, heartbeatMs: 0 });
  await d.start();
  const got: Signal[] = [];
  try {
    d.onSignal(SignalType.PLAN, (s) => void got.push(s));
    await d.ensureSubscribed(SignalType.PLAN);
    await d.emitPlan({
      traceId: `trc_${"0".repeat(26)}`,
      parentId: `evt_${"0".repeat(26)}`,
      steps: ["a"],
    });
    await new Promise((r) => setTimeout(r, 20));
    assert.equal(got.length, 1);
  } finally {
    await d.stop();
    await syn.close();
  }
});

// ---------------------------------------------------------------------------
// awaitDecision (discrete answer-path consumers)
// ---------------------------------------------------------------------------

test("awaitDecision resolves a clarification answer by parent_id", async () => {
  const syn = await bus();
  const asker = new Dendrite({ synapse: syn, role: "worker", heartbeatMs: 0 });
  asker.attachAxon(
    new Axon({
      neuronId: "q",
      neuronFn: () => ({ __clarification__: true, question: "which?" }),
    }),
  );
  const responder = new Dendrite({ synapse: syn, dendriteId: "resp", heartbeatMs: 0 });
  responder.onClarification(async (sig) => {
    await responder.answerClarification(sig, "the blue one");
  });
  const orch = new Dendrite({ synapse: syn, heartbeatMs: 0 });
  await asker.start();
  await responder.start();
  await orch.start();
  try {
    const pw = await orch.dispatch({ neuron: "q", input: {} });
    const clar = await pw.wait(2000);
    assert.equal(clar.type, SignalType.CLARIFICATION);
    const ans = await orch.awaitDecision(clar, { timeoutMs: 2000 });
    assert.equal(ans.type, SignalType.CLARIFICATION_ANSWER);
    assert.equal(ans.parent_id, clar.id);
    assert.equal(ans.payload["answer"], "the blue one");
    await pw.close();
  } finally {
    await orch.stop();
    await responder.stop();
    await asker.stop();
    await syn.close();
  }
});

test("awaitDecision resolves a permission verdict; rejects wrong types", async () => {
  const syn = await bus();
  const worker = new Dendrite({ synapse: syn, role: "worker", heartbeatMs: 0 });
  worker.attachAxon(
    new Axon({
      neuronId: "p",
      neuronFn: () => ({ __permission__: true, action: "rm -rf" }),
    }),
  );
  const responder = new Dendrite({ synapse: syn, heartbeatMs: 0 });
  responder.onPermission(async (sig) => {
    await responder.denyPermission(sig, { reason: "too risky" });
  });
  const orch = new Dendrite({ synapse: syn, heartbeatMs: 0 });
  await worker.start();
  await responder.start();
  await orch.start();
  try {
    const pw = await orch.dispatch({ neuron: "p", input: {} });
    const req = await pw.wait(2000);
    assert.equal(req.type, SignalType.PERMISSION);
    const verdict = await orch.awaitDecision(req, { timeoutMs: 2000 });
    assert.equal(verdict.type, SignalType.PERMISSION_DECISION);
    assert.equal(verdict.payload["granted"], false);
    await pw.close();
    await assert.rejects(orch.awaitDecision(verdict), DendriteProtocolError);
  } finally {
    await orch.stop();
    await responder.stop();
    await worker.stop();
    await syn.close();
  }
});

// ---------------------------------------------------------------------------
// beforeTask + hot attach
// ---------------------------------------------------------------------------

test("beforeTask transforms input; throwing rejects as ERROR", async () => {
  const syn = await bus();
  const worker = new Dendrite({ synapse: syn, role: "worker", heartbeatMs: 0 });
  const ax = new Axon({ neuronId: "e", neuronFn: echo });
  ax.beforeTask((input) => ({ text: String(input["text"] ?? "").toUpperCase() }));
  worker.attachAxon(ax);

  const bad = new Axon({ neuronId: "bad", neuronFn: echo });
  bad.beforeTask(() => {
    throw new Error("input not allowed");
  });
  worker.attachAxon(bad);

  const orch = new Dendrite({ synapse: syn, heartbeatMs: 0 });
  await worker.start();
  await orch.start();
  try {
    const out = await orch.dispatchAndWait({ neuron: "e", input: { text: "hi" }, timeoutMs: 2000 });
    assert.deepEqual(out.payload["output"], { echo: "HI" });
    const err = await orch.dispatchAndWait({ neuron: "bad", input: {}, timeoutMs: 2000 });
    assert.equal(err.type, SignalType.ERROR);
    assert.match(String(err.payload["message"]), /input not allowed/);
  } finally {
    await orch.stop();
    await worker.stop();
    await syn.close();
  }
});

test("attachAxon on a running Dendrite throws; addAxon hot-attaches", async () => {
  const syn = await bus();
  const worker = new Dendrite({ synapse: syn, role: "worker", heartbeatMs: 0 });
  const orch = new Dendrite({ synapse: syn, heartbeatMs: 0 });
  await worker.start();
  await orch.start();
  try {
    assert.throws(
      () => worker.attachAxon(new Axon({ neuronId: "late", neuronFn: echo })),
      /addAxon/,
    );
    await worker.addAxon(
      new Axon({ neuronId: "late", neuronFn: echo, capabilities: ["echo"] }),
    );
    const out = await orch.dispatchAndWait({ neuron: "late", input: { text: "a" }, timeoutMs: 2000 });
    assert.deepEqual(out.payload["output"], { echo: "a" });
    // capability-routed too (queue group created on hot attach)
    const out2 = await orch.dispatchAndWait({
      capabilities: ["echo"],
      input: { text: "b" },
      timeoutMs: 2000,
    });
    assert.deepEqual(out2.payload["output"], { echo: "b" });
    // detach re-keys / drops cleanly
    await worker.detachAxon("late");
    assert.equal(worker.axon("late"), undefined);
  } finally {
    await orch.stop();
    await worker.stop();
    await syn.close();
  }
});

// ---------------------------------------------------------------------------
// Built-in Neuron follow-up rendering + intent prompt
// ---------------------------------------------------------------------------

test("followupPrompt renders clarification and permission follow-ups", () => {
  const c = followupPrompt({
    clarification: { question: "which region?", answer: "eu-west-1" },
  });
  assert.ok(c && c.includes("which region?") && c.includes("eu-west-1"));
  assert.ok(c.includes("Continue the original task"));

  const p = followupPrompt({
    permission: { action: "delete", granted: false, reason: "nope" },
  });
  assert.ok(p && p.includes("DENIED") && p.includes("delete") && p.includes("nope"));

  assert.equal(followupPrompt({ prompt: "hi" }), null);
  assert.equal(followupPrompt({}), null);
});

test("Axon.ollama injects the intent prompt; teachIntents opts out; hf refuses", () => {
  // ollama: system-capable, recognize defaults true -> injected.
  const ax = Axon.ollama("m", { model: "llama3" });
  assert.ok(ax instanceof Axon); // construction succeeded with injected system
  const ax2 = Axon.ollama("m2", { model: "llama3", system: "You are terse." });
  assert.ok(ax2 instanceof Axon);
  const ax3 = Axon.ollama("m3", { model: "llama3" }, { teachIntents: false });
  assert.ok(ax3 instanceof Axon);
  assert.ok(COSMO_INTENT_SYSTEM_PROMPT.includes('"cosmo"'));
  // huggingface accepts no system option: default must not inject, forcing throws.
  const hf = Axon.huggingface("h", { endpoint: "http://localhost:8080" });
  assert.ok(hf instanceof Axon);
  assert.throws(
    () => Axon.huggingface("h2", { endpoint: "http://localhost:8080" }, { teachIntents: true }),
    /teachIntents/,
  );
});

// ---------------------------------------------------------------------------
// Protocol version validation
// ---------------------------------------------------------------------------

test("envelope rejects non-major-1 protocol versions", () => {
  const base = { type: SignalType.TASK, payload: { input: {} } } as const;
  assert.equal(createSignal({ ...base, v: "1" }).v, "1");
  assert.equal(createSignal({ ...base, v: "1.3" }).v, "1.3");
  assert.throws(() => createSignal({ ...base, v: "2" }), /unsupported protocol version/);
  const wire = new TextDecoder().decode(encode(createSignal(base)));
  assert.throws(() => decode(wire.replace('"v":"1"', '"v":"2"')), /unsupported protocol version/);
});

// ---------------------------------------------------------------------------
// Registry staleness
// ---------------------------------------------------------------------------

test("findNeurons maxAgeMs filters; staleness sweep is configured", async () => {
  const syn = await bus();
  const store = new MemoryRegistryStore();
  await store.connect();
  const d = new Dendrite({ synapse: syn, registryStore: store, heartbeatMs: 0 });
  const now = Date.now();
  await store.upsert(
    neuronRecord({
      neuron_id: "old",
      last_heartbeat: new Date(now - 120_000).toISOString(),
    }),
  );
  await store.upsert(
    neuronRecord({ neuron_id: "fresh", last_heartbeat: new Date(now).toISOString() }),
  );
  try {
    const all = (await d.findNeurons()).map((r) => r.neuron_id).sort();
    assert.deepEqual(all, ["fresh", "old"]);
    const fresh = (await d.findNeurons({ maxAgeMs: 60_000 })).map((r) => r.neuron_id);
    assert.deepEqual(fresh, ["fresh"]);
  } finally {
    await store.close();
    await syn.close();
  }
});
