/**
 * Tests for the Effector port: the tool-call standards parsers, the served
 * Effector (onToolCall fall-through / error mapping), Dendrite hosting
 * (attachEffector + TOOL_CALL servicing + effector REGISTER), the caller side
 * (dendrite.callTool over the EffectorClient), and the Axon integration
 * (toolStandard recognition, binding resolution, native dispatch,
 * pure-translation pass-through, and the callTool helper).
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  Axon,
  Dendrite,
  Effector,
  EffectorBinding,
  EffectorNotBound,
  MemorySynapse,
  SignalType,
  ToolOutcome,
  newEventId,
  newTraceId,
  extractToolCall,
  parseClaude,
  parseCodex,
  parseHermes,
  type Json,
  type NeuronHelpers,
  type Signal,
} from "../src/index.js";
import { ServedEffector } from "../src/effector.js";

async function bus(): Promise<MemorySynapse> {
  const syn = new MemorySynapse();
  await syn.connect();
  return syn;
}

function echoEffector(id = "fx1", kind = "echo"): ServedEffector {
  const fx = Effector.serve({ effectorId: id, effectorKind: kind });
  fx.onToolCall((tool, args) => {
    if (tool === "echo") return { echoed: args["value"] };
    if (tool === "boom") throw new Error("kapow");
    return null;
  });
  return fx;
}

// ---------------------------------------------------------------------------
// Standards parsers
// ---------------------------------------------------------------------------

test("parseHermes matches <tool_call> tags and ignores prose", () => {
  const hit = parseHermes(
    'Sure!\n<tool_call>\n{"name": "read", "arguments": {"path": "a.py"}}\n</tool_call>',
  );
  assert.ok(hit);
  assert.equal(hit.tool, "read");
  assert.deepEqual(hit.args, { path: "a.py" });
  assert.equal(hit.callId, null);
  assert.equal(parseHermes("just prose"), null);
});

test("parseClaude matches tool_use blocks, plain JSON never misfires", () => {
  const hit = parseClaude('{"type": "tool_use", "id": "toolu_01", "name": "read", "input": {"p": 1}}');
  assert.ok(hit);
  assert.equal(hit.tool, "read");
  assert.equal(hit.callId, "toolu_01");
  assert.equal(parseClaude('{"answer": 42}'), null);
});

test("parseCodex: tool_calls array, string-encoded args, bare + llama shapes", () => {
  const arr = parseCodex(
    '{"tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "read", "arguments": "{\\"p\\": 1}"}}]}',
  );
  assert.ok(arr);
  assert.equal(arr.tool, "read");
  assert.deepEqual(arr.args, { p: 1 });
  assert.equal(arr.callId, "call_1");

  const bare = parseCodex('{"name": "read", "arguments": {"p": 2}}');
  assert.ok(bare);
  assert.deepEqual(bare.args, { p: 2 });

  const llama = parseCodex('{"name": "read", "parameters": {"p": 3}}');
  assert.ok(llama);
  assert.deepEqual(llama.args, { p: 3 });

  // Exact-keys guard: extra keys mean it is not a call.
  assert.equal(parseCodex('{"name": "x", "arguments": {}, "note": "hi"}'), null);
  // A "parameters" field without the type marker must never misfire.
  assert.equal(parseCodex('{"query": 1, "parameters": {"p": 1}}'), null);
});

test("parseCodex: fenced JSON and trailing junk tolerated", () => {
  const fenced = parseCodex('thinking...\n```json\n{"name": "read", "arguments": {"p": 1}}\n```\ndone');
  assert.ok(fenced);
  assert.equal(fenced.tool, "read");
  const junk = parseCodex('{"name": "read", "arguments": {"p": 1}}  # done');
  assert.ok(junk);
});

test("extractToolCall accepts {response} shape and unknown standards", () => {
  const hit = extractToolCall({ response: '{"name": "read", "arguments": {}}' }, "codex");
  assert.ok(hit);
  assert.equal(extractToolCall({ response: "{}" }, "nope"), null);
  assert.equal(extractToolCall(42, "codex"), null);
});

// ---------------------------------------------------------------------------
// Served Effector
// ---------------------------------------------------------------------------

test("ServedEffector: first non-null answers, throw maps to error, unhandled tool errors", async () => {
  const fx = echoEffector();
  const ok = await fx.invoke("echo", { value: 7 }, { callId: "c1" });
  assert.ok(ok.ok);
  assert.deepEqual(ok.result, { echoed: 7 });
  assert.equal(ok.callId, "c1");
  assert.equal(ok.effectorId, "fx1");

  const boom = await fx.invoke("boom", {});
  assert.ok(!boom.ok);
  assert.match(boom.error ?? "", /kapow/);

  const miss = await fx.invoke("unknown", {});
  assert.ok(!miss.ok);
  assert.match(miss.error ?? "", /unhandled tool/);
});

test("ServedEffector: handler may return a ready-made ToolOutcome", async () => {
  const fx = Effector.serve({ effectorId: "fx2" });
  fx.onToolCall((tool) => new ToolOutcome({ tool, error: "refused" }));
  const out = await fx.invoke("anything", {});
  assert.equal(out.error, "refused");
});

// ---------------------------------------------------------------------------
// Dendrite hosting + caller side
// ---------------------------------------------------------------------------

test("Dendrite hosts an Effector: callTool round-trips TOOL_CALL/TOOL_RESULT", async () => {
  const syn = await bus();
  const host = new Dendrite({ synapse: syn, dendriteId: "host", heartbeatMs: 0 });
  await host.attachEffector(echoEffector());
  await host.start();
  try {
    const out = await host.callTool({
      effectorId: "fx1",
      tool: "echo",
      args: { value: "hi" },
      deadlineMs: 2_000,
    });
    assert.ok(out.ok);
    assert.deepEqual(out.result, { echoed: "hi" });
    assert.equal(out.effectorId, "fx1");
  } finally {
    await host.stop();
    await syn.close();
  }
});

test("TOOL_CALL routed by effectorKind; tool errors ride TOOL_RESULT", async () => {
  const syn = await bus();
  const host = new Dendrite({ synapse: syn, dendriteId: "host", heartbeatMs: 0 });
  await host.attachEffector(echoEffector());
  await host.start();
  try {
    const out = await host.callTool({
      effectorKind: "echo",
      tool: "boom",
      args: {},
      deadlineMs: 2_000,
    });
    assert.ok(!out.ok);
    assert.match(out.error ?? "", /kapow/);
  } finally {
    await host.stop();
    await syn.close();
  }
});

test("hosted Effector announces itself with REGISTER role=effector", async () => {
  const syn = await bus();
  const seen: Signal[] = [];
  const observer = new Dendrite({ synapse: syn, dendriteId: "obs", heartbeatMs: 0 });
  observer.onSignal(SignalType.REGISTER, (sig) => {
    seen.push(sig);
  });
  await observer.start();
  const host = new Dendrite({ synapse: syn, dendriteId: "host", heartbeatMs: 0 });
  await host.attachEffector(echoEffector());
  await host.start();
  try {
    const reg = seen.find((s) => s.payload["role"] === "effector");
    assert.ok(reg, "expected an effector REGISTER");
    assert.equal(reg.directed?.id, "fx1");
    assert.equal(reg.directed?.type, "echo");
  } finally {
    await host.stop();
    await observer.stop();
    await syn.close();
  }
});

test("effector.host.on* registrations replay onto the hosting Dendrite", async () => {
  const syn = await bus();
  const fx = echoEffector("fx-host");
  const finals: Signal[] = [];
  fx.host.onFinal((sig) => {
    finals.push(sig);
  });
  const host = new Dendrite({ synapse: syn, dendriteId: "host", heartbeatMs: 0 });
  await host.attachEffector(fx);
  await host.start();
  try {
    assert.equal(fx.dendrite, host);
    await host.emitFinal({ traceId: newTraceId(), parentId: newEventId(), result: { done: true } });
    await new Promise((r) => setTimeout(r, 20));
    assert.equal(finals.length, 1);
  } finally {
    await host.stop();
    await syn.close();
  }
});

// ---------------------------------------------------------------------------
// Axon integration
// ---------------------------------------------------------------------------

test("Axon: effectors without toolStandard throws; unknown standard throws", () => {
  const b = new EffectorBinding({ name: "fs", directedId: "fx1" });
  assert.throws(
    () => new Axon({ neuronId: "n", neuronFn: () => ({}), effectors: [b] }),
    /requires toolStandard/,
  );
  assert.throws(
    () => new Axon({ neuronId: "n", neuronFn: () => ({}), toolStandard: "morse" }),
    /unknown toolStandard/,
  );
});

test("EffectorBinding requires directedId or directedType", () => {
  assert.throws(() => new EffectorBinding({ name: "fs" }), /requires directedId/);
});

test("Axon native dispatch: recognised call is executed, observation rides AGENT_OUTPUT", async () => {
  const syn = await bus();
  const host = new Dendrite({ synapse: syn, dendriteId: "host", heartbeatMs: 0, role: "worker" });
  await host.attachEffector(echoEffector());
  const axon = new Axon({
    neuronId: "agent",
    neuronFn: () => ({ response: '{"name": "echo", "arguments": {"value": 9}}' }),
    toolStandard: "codex",
    effectors: [new EffectorBinding({ name: "fx", directedId: "fx1", tools: ["echo"] })],
  });
  host.attachAxon(axon);
  await host.start();

  const orch = new Dendrite({ synapse: syn, dendriteId: "orch", heartbeatMs: 0 });
  await orch.start();
  try {
    const reply = await orch.dispatchAndWait({
      neuron: "agent",
      input: {},
      timeoutMs: 5_000,
    });
    assert.equal(reply.type, SignalType.AGENT_OUTPUT);
    const output = reply.payload["output"] as Json;
    assert.equal(output["tool"], "echo");
    assert.deepEqual(output["result"], { echoed: 9 });
    assert.equal(output["effector_id"], "fx1");
  } finally {
    await orch.stop();
    await host.stop();
    await syn.close();
  }
});

test("Axon pure translation: no bindings passes the call through unexecuted", async () => {
  const syn = await bus();
  const host = new Dendrite({ synapse: syn, dendriteId: "host", heartbeatMs: 0, role: "worker" });
  const axon = new Axon({
    neuronId: "agent",
    neuronFn: () => ({
      response: '<tool_call>{"name": "read", "arguments": {"path": "a"}}</tool_call>',
    }),
    toolStandard: "hermes",
  });
  host.attachAxon(axon);
  await host.start();
  const orch = new Dendrite({ synapse: syn, dendriteId: "orch", heartbeatMs: 0 });
  await orch.start();
  try {
    const reply = await orch.dispatchAndWait({ neuron: "agent", input: {}, timeoutMs: 5_000 });
    assert.equal(reply.type, SignalType.AGENT_OUTPUT);
    const output = reply.payload["output"] as Json;
    assert.equal(output["tool"], "read");
    assert.deepEqual(output["args"], { path: "a" });
    assert.equal(output["result"], undefined);
    assert.equal(output["error"], undefined);
  } finally {
    await orch.stop();
    await host.stop();
    await syn.close();
  }
});

test("Axon: unserved tool reports error in output, never an ERROR signal", async () => {
  const syn = await bus();
  const host = new Dendrite({ synapse: syn, dendriteId: "host", heartbeatMs: 0, role: "worker" });
  await host.attachEffector(echoEffector());
  const axon = new Axon({
    neuronId: "agent",
    neuronFn: () => ({ response: '{"name": "write", "arguments": {}}' }),
    toolStandard: "codex",
    effectors: [
      new EffectorBinding({ name: "a", directedId: "fx1", tools: ["echo"] }),
      new EffectorBinding({ name: "b", directedType: "other", tools: ["noop"] }),
    ],
  });
  host.attachAxon(axon);
  await host.start();
  const orch = new Dendrite({ synapse: syn, dendriteId: "orch", heartbeatMs: 0 });
  await orch.start();
  try {
    const reply = await orch.dispatchAndWait({ neuron: "agent", input: {}, timeoutMs: 5_000 });
    assert.equal(reply.type, SignalType.AGENT_OUTPUT);
    const output = reply.payload["output"] as Json;
    assert.match(String(output["error"]), /no effector binding serves tool 'write'/);
  } finally {
    await orch.stop();
    await host.stop();
    await syn.close();
  }
});

test("Neuron callTool helper resolves bindings and enforces the whitelist", async () => {
  const syn = await bus();
  const host = new Dendrite({ synapse: syn, dendriteId: "host", heartbeatMs: 0, role: "worker" });
  await host.attachEffector(echoEffector());
  let unboundErr: unknown = null;
  const axon = new Axon({
    neuronId: "agent",
    neuronFn: async (_i: Json, _c: unknown[], helpers?: NeuronHelpers) => {
      assert.ok(helpers);
      try {
        await helpers.callTool("nope", { tool: "echo" });
      } catch (err) {
        unboundErr = err;
      }
      const out = (await helpers.callTool("fx", {
        tool: "echo",
        args: { value: 3 },
        deadlineMs: 2_000,
      })) as ToolOutcome;
      return { got: out.result };
    },
    toolStandard: "codex",
    effectors: [new EffectorBinding({ name: "fx", directedId: "fx1" })],
  });
  host.attachAxon(axon);
  await host.start();
  const orch = new Dendrite({ synapse: syn, dendriteId: "orch", heartbeatMs: 0 });
  await orch.start();
  try {
    const reply = await orch.dispatchAndWait({ neuron: "agent", input: {}, timeoutMs: 5_000 });
    assert.equal(reply.type, SignalType.AGENT_OUTPUT);
    assert.deepEqual(reply.payload["output"], { got: { echoed: 3 } });
    assert.ok(unboundErr instanceof EffectorNotBound);
  } finally {
    await orch.stop();
    await host.stop();
    await syn.close();
  }
});
