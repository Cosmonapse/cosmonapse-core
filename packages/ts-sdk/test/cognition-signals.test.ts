import assert from "node:assert/strict";
import { test } from "node:test";

import {
  consensusSignal,
  contextSyncSignal,
  discoverSignal,
  escalationSignal,
  planSignal,
  SYNAPSE_TYPES,
  SignalType,
  thoughtDeltaSignal,
  toolCallSignal,
  toolResultSignal,
  validateSignal,
} from "../src/index.js";

test("planSignal carries steps and optional rationale", () => {
  const s = planSignal({
    traceId: "trc_x",
    parentId: "evt_p",
    steps: [{ id: 1 }, { id: 2 }],
    rationale: "because",
  });
  validateSignal(s);
  assert.equal(s.type, SignalType.PLAN);
  assert.deepEqual(s.payload["steps"], [{ id: 1 }, { id: 2 }]);
  assert.equal(s.payload["rationale"], "because");
  // omitted optional must be absent, not null
  const s2 = planSignal({ traceId: "trc_x", parentId: "evt_p", steps: [] });
  assert.ok(!("rationale" in s2.payload));
});

test("thoughtDeltaSignal streams delta + seq", () => {
  const s = thoughtDeltaSignal({ traceId: "trc_x", parentId: "evt_p", delta: "tok", seq: 3 });
  assert.equal(s.type, SignalType.THOUGHT_DELTA);
  assert.equal(s.payload["delta"], "tok");
  assert.equal(s.payload["seq"], 3);
});

test("toolCallSignal + toolResultSignal mirror the Python shapes", () => {
  const call = toolCallSignal({
    traceId: "trc_x",
    parentId: "evt_p",
    tool: "search",
    args: { q: "hi" },
    callId: "c1",
  });
  assert.equal(call.type, SignalType.TOOL_CALL);
  assert.equal(call.payload["tool"], "search");
  assert.deepEqual(call.payload["args"], { q: "hi" });
  assert.equal(call.payload["call_id"], "c1");

  const ok = toolResultSignal({ traceId: "trc_x", parentId: "evt_p", tool: "search", result: [1, 2] });
  assert.deepEqual(ok.payload["result"], [1, 2]);
  assert.ok(!("error" in ok.payload));

  const bad = toolResultSignal({ traceId: "trc_x", parentId: "evt_p", tool: "search", error: "boom" });
  assert.equal(bad.payload["error"], "boom");
  assert.ok(!("result" in bad.payload));
});

test("escalation / consensus / contextSync shapes", () => {
  const esc = escalationSignal({ traceId: "trc_x", parentId: "evt_p", reason: "stuck", target: "boss" });
  assert.equal(esc.type, SignalType.ESCALATION);
  assert.equal(esc.payload["reason"], "stuck");
  assert.equal(esc.payload["target"], "boss");

  const con = consensusSignal({
    traceId: "trc_x",
    parentId: "evt_p",
    members: ["a", "b"],
    verdict: "agree",
    votes: { a: 1, b: 1 },
  });
  assert.deepEqual(con.payload["members"], ["a", "b"]);
  assert.equal(con.payload["verdict"], "agree");

  const ctx = contextSyncSignal({ traceId: "trc_x", parentId: "evt_p", snapshot: { k: "v" }, version: "2" });
  assert.deepEqual(ctx.payload["snapshot"], { k: "v" });
  assert.equal(ctx.payload["version"], "2");
});

test("discoverSignal defaults trace_id and is a synapse-side type", () => {
  const s = discoverSignal({ capabilities: ["summarize"] });
  assert.equal(s.type, SignalType.DISCOVER);
  assert.ok(s.trace_id.startsWith("trc_"));
  assert.equal(s.parent_id, null);
  assert.deepEqual(s.payload["capabilities"], ["summarize"]);
  // no-arg form is valid
  assert.equal(discoverSignal().type, SignalType.DISCOVER);
});

test("all cognition types are emittable by a Dendrite (SYNAPSE_TYPES)", () => {
  for (const t of [
    SignalType.PLAN,
    SignalType.THOUGHT_DELTA,
    SignalType.TOOL_CALL,
    SignalType.TOOL_RESULT,
    SignalType.ESCALATION,
    SignalType.CONSENSUS,
    SignalType.CONTEXT_SYNC,
    SignalType.DISCOVER,
  ]) {
    assert.ok(SYNAPSE_TYPES.has(t), `${t} should be in SYNAPSE_TYPES`);
  }
});
