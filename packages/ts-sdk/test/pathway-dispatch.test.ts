/**
 * Tests for the unified-dispatch port: Pathway, dispatch family,
 * terminal-handler finalize, capability routing, offer/bid + auto-bid.
 * Mirrors the Python tests/test_finalize.py + parts of test_event_driven.py.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  Axon,
  Dendrite,
  DendriteProtocolError,
  MemorySynapse,
  PathwayClosedError,
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

function workerWith(
  syn: MemorySynapse,
  opts: { caps?: string[]; fn?: (i: Json, c: unknown[]) => Json | Promise<Json>; autoBid?: boolean } = {},
): Dendrite {
  const worker = new Dendrite({
    synapse: syn,
    role: "worker",
    dendriteId: "w",
    heartbeatMs: 0,
    ...(opts.autoBid !== undefined ? { autoBid: opts.autoBid } : {}),
  });
  worker.attachAxon(
    new Axon({
      neuronId: "echoer",
      neuronFn: opts.fn ?? ((i) => echo(i)),
      capabilities: opts.caps ?? ["echo"],
    }),
  );
  return worker;
}

// ---------------------------------------------------------------------------
// dispatch + wait/subscribe/iterate
// ---------------------------------------------------------------------------

test("dispatchAndWait resolves with AGENT_OUTPUT (scope all)", async () => {
  const syn = await bus();
  const worker = workerWith(syn);
  const orch = new Dendrite({ synapse: syn, heartbeatMs: 0 });
  await worker.start();
  await orch.start();
  try {
    const sig = await orch.dispatchAndWait({
      neuron: "echoer",
      input: { text: "hi" },
      timeoutMs: 2000,
    });
    assert.equal(sig.type, SignalType.AGENT_OUTPUT);
    assert.deepEqual(sig.payload["output"], { echo: "hi" });
  } finally {
    await orch.stop();
    await worker.stop();
    await syn.close();
  }
});

test("scope terminal: worker promotes AGENT_OUTPUT to FINAL (option b)", async () => {
  const syn = await bus();
  const worker = workerWith(syn);
  const orch = new Dendrite({ synapse: syn, heartbeatMs: 0 });
  await worker.start();
  await orch.start();
  try {
    const sig = await orch.dispatchAndWait({
      neuron: "echoer",
      input: { text: "hi" },
      scope: "terminal",
      timeoutMs: 2000,
    });
    assert.equal(sig.type, SignalType.FINAL);
    assert.deepEqual(sig.payload["result"], { echo: "hi" });
    assert.equal(sig.directed?.id, "echoer"); // attributed to the neuron
  } finally {
    await orch.stop();
    await worker.stop();
    await syn.close();
  }
});

test("FINAL is parented to the AGENT_OUTPUT (lineage TASK -> OUTPUT -> FINAL)", async () => {
  const syn = await bus();
  const worker = workerWith(syn);
  const orch = new Dendrite({ synapse: syn, heartbeatMs: 0 });
  await worker.start();
  await orch.start();
  try {
    const pw = await orch.dispatch({ neuron: "echoer", input: { text: "x" }, finalize: true });
    const out = await pw.waitFor(SignalType.AGENT_OUTPUT, 2000);
    const fin = await pw.waitFor(SignalType.FINAL, 2000);
    assert.equal(fin.parent_id, out.id);
    assert.equal(fin.trace_id, out.trace_id);
  } finally {
    await orch.stop();
    await worker.stop();
    await syn.close();
  }
});

test("default scope all emits NO FINAL (multi-step orchestration stays safe)", async () => {
  const syn = await bus();
  const worker = workerWith(syn);
  const orch = new Dendrite({ synapse: syn, heartbeatMs: 0 });
  await worker.start();
  await orch.start();
  try {
    const pw = await orch.dispatch({ neuron: "echoer", input: { text: "x" } });
    await pw.waitFor(SignalType.AGENT_OUTPUT, 2000);
    await assert.rejects(
      pw.waitFor(SignalType.FINAL, 150),
      (e: Error) => e.name === "TimeoutError",
    );
    assert.equal(pw.closed, false); // no premature auto-close
    await pw.close();
  } finally {
    await orch.stop();
    await worker.stop();
    await syn.close();
  }
});

test("explicit finalize: false on terminal scope suppresses promotion", async () => {
  const syn = await bus();
  const worker = workerWith(syn);
  const orch = new Dendrite({ synapse: syn, heartbeatMs: 0 });
  await worker.start();
  await orch.start();
  try {
    await assert.rejects(
      orch.dispatchAndWait({
        neuron: "echoer",
        input: { text: "x" },
        scope: "terminal",
        finalize: false,
        timeoutMs: 200,
      }),
      (e: Error) => e.name === "TimeoutError",
    );
  } finally {
    await orch.stop();
    await worker.stop();
    await syn.close();
  }
});

test("ERROR replies are not promoted; pathway auto-closes", async () => {
  const syn = await bus();
  const worker = workerWith(syn, {
    fn: () => {
      throw new Error("nope");
    },
  });
  const orch = new Dendrite({ synapse: syn, heartbeatMs: 0 });
  await worker.start();
  await orch.start();
  try {
    const pw = await orch.dispatch({ neuron: "echoer", input: {}, finalize: true });
    const err = await pw.wait(2000);
    assert.equal(err.type, SignalType.ERROR);
    // ERROR auto-closed the Pathway: a further wait rejects with closed.
    await assert.rejects(pw.wait(100), PathwayClosedError);
  } finally {
    await orch.stop();
    await worker.stop();
    await syn.close();
  }
});

test("streaming shape: for await sees output then FINAL", async () => {
  const syn = await bus();
  const worker = workerWith(syn);
  const orch = new Dendrite({ synapse: syn, heartbeatMs: 0 });
  await worker.start();
  await orch.start();
  try {
    const pw = await orch.dispatch({ neuron: "echoer", input: { text: "s" }, finalize: true });
    const seen: SignalType[] = [];
    for await (const sig of pw) seen.push(sig.type);
    assert.deepEqual(seen, [SignalType.AGENT_OUTPUT, SignalType.FINAL]);
  } finally {
    await orch.stop();
    await worker.stop();
    await syn.close();
  }
});

// ---------------------------------------------------------------------------
// roles + capability routing
// ---------------------------------------------------------------------------

test("worker role cannot dispatch", async () => {
  const syn = await bus();
  const worker = new Dendrite({ synapse: syn, role: "worker", heartbeatMs: 0 });
  await worker.start();
  try {
    await assert.rejects(
      worker.dispatchTask({ neuron: "x", input: {} }),
      DendriteProtocolError,
    );
    await assert.rejects(worker.dispatch({ neuron: "x", input: {} }), DendriteProtocolError);
  } finally {
    await worker.stop();
    await syn.close();
  }
});

test("capability-routed dispatch reaches a covering Axon (subset match)", async () => {
  const syn = await bus();
  const worker = workerWith(syn, { caps: ["echo", "english"] });
  const orch = new Dendrite({ synapse: syn, heartbeatMs: 0 });
  await worker.start();
  await orch.start();
  try {
    const sig = await orch.dispatchAndWait({
      capabilities: ["echo"],
      input: { text: "cap" },
      scope: "terminal",
      timeoutMs: 2000,
    });
    assert.equal(sig.type, SignalType.FINAL);
    assert.deepEqual(sig.payload["result"], { echo: "cap" });
  } finally {
    await orch.stop();
    await worker.stop();
    await syn.close();
  }
});

// ---------------------------------------------------------------------------
// offer / bid
// ---------------------------------------------------------------------------

test("stock worker auto-bids; offer with terminal scope finalizes through award", async () => {
  const syn = await bus();
  const worker = workerWith(syn);
  const orch = new Dendrite({ synapse: syn, heartbeatMs: 0 });
  await worker.start();
  await orch.start();
  try {
    const pw = await orch.dispatchOffer({
      input: { text: "won" },
      capabilities: ["echo"],
      deadlineMs: 300,
      scope: "terminal",
    });
    const fin = await pw.wait(2000);
    assert.equal(fin.type, SignalType.FINAL);
    assert.deepEqual(fin.payload["result"], { echo: "won" });
  } finally {
    await orch.stop();
    await worker.stop();
    await syn.close();
  }
});

test("no matching capability -> offer times out (auto-bid stays silent)", async () => {
  const syn = await bus();
  const worker = workerWith(syn);
  const orch = new Dendrite({ synapse: syn, heartbeatMs: 0 });
  await worker.start();
  await orch.start();
  try {
    await assert.rejects(
      orch.dispatchOffer({ input: {}, capabilities: ["not-echo"], deadlineMs: 100 }),
      (e: Error) => e.name === "TimeoutError",
    );
  } finally {
    await orch.stop();
    await worker.stop();
    await syn.close();
  }
});

test("user onTaskOffer handler suppresses the auto-bidder", async () => {
  const syn = await bus();
  const worker = workerWith(syn);
  worker.onTaskOffer(() => {
    /* deliberately never bids */
  });
  const orch = new Dendrite({ synapse: syn, heartbeatMs: 0 });
  const bids: Signal[] = [];
  orch.onBid((s) => void bids.push(s));
  await worker.start();
  await orch.start();
  try {
    await assert.rejects(
      orch.dispatchOffer({ input: {}, capabilities: ["echo"], deadlineMs: 100 }),
      (e: Error) => e.name === "TimeoutError",
    );
    assert.equal(bids.length, 0);
  } finally {
    await orch.stop();
    await worker.stop();
    await syn.close();
  }
});

test("lowest_cost drains the window and declines the loser", async () => {
  const syn = await bus();
  const a = new Dendrite({ synapse: syn, role: "worker", heartbeatMs: 0, autoBid: false });
  const b = new Dendrite({ synapse: syn, role: "worker", heartbeatMs: 0, autoBid: false });
  const fn = (): Json => ({ ok: true });
  a.attachAxon(new Axon({ neuronId: "a", neuronFn: fn, capabilities: ["x"] }));
  b.attachAxon(new Axon({ neuronId: "b", neuronFn: fn, capabilities: ["x"] }));
  a.onTaskOffer(async (offer) => void (await a.bid(offer, { neuron: "a", cost: 1 })));
  b.onTaskOffer(async (offer) => void (await b.bid(offer, { neuron: "b", cost: 9 })));
  const declined: Signal[] = [];
  b.onTaskDeclined((s) => void declined.push(s), { neuron: "b" });
  const orch = new Dendrite({ synapse: syn, heartbeatMs: 0 });
  await a.start();
  await b.start();
  await orch.start();
  try {
    const pw = await orch.dispatchOffer({
      input: {},
      capabilities: ["x"],
      deadlineMs: 200,
      select: "lowest_cost",
    });
    const out = await pw.wait(2000);
    assert.equal(out.type, SignalType.AGENT_OUTPUT);
    assert.equal(out.directed?.id, "a"); // cheapest bid won
    await pw.close();
    await new Promise((r) => setTimeout(r, 50));
    assert.equal(declined.length, 1);
    assert.equal(declined[0]!.payload["reason"], "not selected");
  } finally {
    await orch.stop();
    await a.stop();
    await b.stop();
    await syn.close();
  }
});
