/**
 * Tests for the `axon.host` deferred-decorator surface.
 *
 * Covers:
 *  - host.onToolCall queued at declaration time is applied to the HOSTING
 *    Dendrite at start (REGISTER) with the subscription ensured - the
 *    handler fires with no manual ensureSubscribed.
 *  - Filters forward unchanged (neuron= gating).
 *  - Registrations are applied exactly once per Axon (re-announce safe).
 *  - addAxon (live attach) applies queued registrations too.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  Axon,
  Dendrite,
  MemorySynapse,
  newEventId,
  newTraceId,
  SignalType,
  type Json,
  type Signal,
} from "../src/index.js";

const noopNeuron = async (_input: Json, _ctx: unknown[]): Promise<Json> => ({ ok: true });

function toolAxon(): Axon {
  return new Axon({ neuronId: "toolbox", neuronFn: noopNeuron, capabilities: ["hammer"] });
}

test("host.onToolCall is applied on start and fires without manual wiring", async () => {
  const syn = new MemorySynapse();
  await syn.connect();

  const axon = toolAxon();
  const got: Signal[] = [];
  axon.host.onToolCall((sig) => void got.push(sig), { neuron: "hammer" });

  const host = new Dendrite({
    synapse: syn, dendriteId: "tool-node", role: "worker", heartbeatMs: 0,
  });
  host.attachAxon(axon);
  const caller = new Dendrite({ synapse: syn, dendriteId: "caller", heartbeatMs: 0 });

  await host.start();
  await caller.start();
  try {
    await caller.emitToolCall({
      traceId: newTraceId(), parentId: newEventId(),
      tool: "bang", args_: { n: 3 }, callId: "c1", neuron: "hammer",
    });
    await new Promise((r) => setTimeout(r, 0));

    assert.equal(got.length, 1);
    assert.equal(got[0]!.payload["tool"], "bang");
    assert.equal(got[0]!.payload["call_id"] ?? got[0]!.payload["callId"], "c1");
  } finally {
    await caller.stop();
    await host.stop();
    await syn.close();
  }
});

test("host filter gates other capabilities", async () => {
  const syn = new MemorySynapse();
  await syn.connect();

  const axon = toolAxon();
  const fired: string[] = [];
  axon.host.onToolCall((sig) => void fired.push(String(sig.payload["tool"])), {
    neuron: "hammer",
  });

  const host = new Dendrite({
    synapse: syn, dendriteId: "tool-node", role: "worker", heartbeatMs: 0,
  });
  host.attachAxon(axon);
  const caller = new Dendrite({ synapse: syn, dendriteId: "caller", heartbeatMs: 0 });

  await host.start();
  await caller.start();
  try {
    await caller.emitToolCall({
      traceId: newTraceId(), parentId: newEventId(),
      tool: "saw", args_: {}, neuron: "screwdriver", // someone else's capability
    });
    await caller.emitToolCall({
      traceId: newTraceId(), parentId: newEventId(),
      tool: "bang", args_: {}, neuron: "hammer",
    });
    await new Promise((r) => setTimeout(r, 10));

    assert.deepEqual(fired, ["bang"]);
  } finally {
    await caller.stop();
    await host.stop();
    await syn.close();
  }
});

test("host registrations are applied exactly once, and addAxon applies live", async () => {
  const syn = new MemorySynapse();
  await syn.connect();

  // addAxon on a RUNNING dendrite must also replay queued registrations.
  const axon = toolAxon();
  const got: Signal[] = [];
  axon.host.onToolCall((sig) => void got.push(sig), { neuron: "hammer" });

  const host = new Dendrite({
    synapse: syn, dendriteId: "tool-node", role: "worker", heartbeatMs: 0,
  });
  const caller = new Dendrite({ synapse: syn, dendriteId: "caller", heartbeatMs: 0 });
  await host.start();
  await caller.start();
  try {
    await host.addAxon(axon);          // live attach: applies host regs
    await host.addAxon(new Axon({      // triggers another REGISTER pass
      neuronId: "other", neuronFn: noopNeuron, capabilities: ["misc"],
    }));

    await caller.emitToolCall({
      traceId: newTraceId(), parentId: newEventId(),
      tool: "bang", args_: {}, neuron: "hammer",
    });
    await new Promise((r) => setTimeout(r, 10));

    // Applied once: exactly one handler fired exactly once.
    assert.equal(got.length, 1);
  } finally {
    await caller.stop();
    await host.stop();
    await syn.close();
  }
});

test("host.onSignal is the generic escape hatch", () => {
  const axon = toolAxon();
  const fn = axon.host.onSignal(SignalType.CONSENSUS, () => undefined);
  assert.equal(typeof fn, "function");
});
