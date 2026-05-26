import assert from "node:assert/strict";
import { test } from "node:test";

import { MemorySynapse } from "../src/synapse.js";
import { SignalType, taskSignal, type Signal } from "../src/index.js";

function task(input = {}): Signal {
  return taskSignal({ input });
}

test("publish requires a connected synapse", async () => {
  const syn = new MemorySynapse();
  await assert.rejects(() => syn.publish("a.b.c", task()), /not connected/);
});

test("fan-out: every solo subscriber receives the message", async () => {
  const syn = new MemorySynapse();
  await syn.connect();
  const got: string[] = [];
  await syn.subscribe("cosmonapse.default.TASK", () => void got.push("a"));
  await syn.subscribe("cosmonapse.default.TASK", () => void got.push("b"));
  await syn.publish("cosmonapse.default.TASK", task());
  assert.deepEqual(got.sort(), ["a", "b"]);
});

test("wildcards: * matches one token, > matches the rest", async () => {
  assert.equal(MemorySynapse.matches("cosmonapse.*.TASK", "cosmonapse.team_a.TASK"), true);
  assert.equal(MemorySynapse.matches("cosmonapse.*.TASK", "cosmonapse.a.b.TASK"), false);
  assert.equal(MemorySynapse.matches("cosmonapse.>", "cosmonapse.a.b.TASK"), true);
  assert.equal(MemorySynapse.matches("cosmonapse.team_a.TASK", "cosmonapse.team_b.TASK"), false);

  const syn = new MemorySynapse();
  await syn.connect();
  let count = 0;
  await syn.subscribe("cosmonapse.>", () => void count++);
  await syn.publish("cosmonapse.default.TASK", task());
  await syn.publish("cosmonapse.default.AGENT_OUTPUT", task());
  assert.equal(count, 2);
});

test("queue group: only one member receives each message, round-robin", async () => {
  const syn = new MemorySynapse();
  await syn.connect();
  const hits = [0, 0];
  await syn.subscribe("cosmonapse.default.TASK", () => void hits[0]++, { queueGroup: "workers" });
  await syn.subscribe("cosmonapse.default.TASK", () => void hits[1]++, { queueGroup: "workers" });
  for (let i = 0; i < 4; i++) await syn.publish("cosmonapse.default.TASK", task());
  assert.equal(hits[0]! + hits[1]!, 4); // each message delivered exactly once
  assert.equal(hits[0], 2);
  assert.equal(hits[1], 2);
});

test("unsubscribe stops delivery", async () => {
  const syn = new MemorySynapse();
  await syn.connect();
  let count = 0;
  const sub = await syn.subscribe("cosmonapse.default.TASK", () => void count++);
  await syn.publish("cosmonapse.default.TASK", task());
  await sub.unsubscribe();
  await syn.publish("cosmonapse.default.TASK", task());
  assert.equal(count, 1);
});

test("request/reply: responder answers via _reply_to", async () => {
  const syn = new MemorySynapse();
  await syn.connect();
  await syn.subscribe("cosmonapse.default.TASK", async (incoming) => {
    const answer = taskSignal({ input: { echoed: incoming.payload["input"] } });
    answer.type = SignalType.AGENT_OUTPUT;
    await syn.replyTo(incoming, answer);
  });
  const reply = await syn.request("cosmonapse.default.TASK", task({ n: 1 }), { timeoutMs: 1000 });
  assert.equal(reply.type, "AGENT_OUTPUT");
  assert.deepEqual(reply.payload["input"], { echoed: { n: 1 } });
});

test("request: rejects on timeout when nobody replies", async () => {
  const syn = new MemorySynapse();
  await syn.connect();
  await assert.rejects(
    () => syn.request("cosmonapse.default.TASK", task(), { timeoutMs: 50 }),
    /within 50ms/,
  );
});
