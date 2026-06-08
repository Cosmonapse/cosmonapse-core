import assert from "node:assert/strict";
import { test } from "node:test";

import {
  createSignal,
  decode,
  encode,
  reply,
  SignalType,
  taskSignal,
  validateSignal,
} from "../src/index.js";

test("createSignal fills protocol defaults and validates", () => {
  const s = createSignal({ type: SignalType.TASK });
  assert.equal(s.v, "1");
  assert.ok(s.id.startsWith("evt_"));
  assert.ok(s.trace_id.startsWith("trc_"));
  assert.equal(s.parent_id, null);
  assert.equal(s.type, "TASK");
});

test("validateSignal rejects bad id prefixes", () => {
  assert.throws(() =>
    validateSignal({
      v: "1",
      id: "bad",
      trace_id: "trc_x",
      parent_id: null,
      type: SignalType.TASK,
      directed: null,
      ts: new Date().toISOString(),
      payload: {},
      meta: {},
    }),
  );
});

test("encode/decode round-trips", () => {
  const s = taskSignal({ input: { hello: "world" }, directed: { id: "demo" } });
  const back = decode(encode(s));
  assert.deepEqual(back.payload, { input: { hello: "world" } });
  assert.equal(back.trace_id, s.trace_id);
});

test("reply shares trace_id and links parent_id", () => {
  const task = taskSignal({ input: {} });
  const out = reply(task, { type: SignalType.AGENT_OUTPUT, directed: { id: "n1" } });
  assert.equal(out.trace_id, task.trace_id);
  assert.equal(out.parent_id, task.id);
});
