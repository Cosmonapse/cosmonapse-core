import assert from "node:assert/strict";
import { test } from "node:test";

import { MemoryRegistryStore, neuronRecord } from "../src/index.js";

test("upsert + get round-trips a record", async () => {
  const s = new MemoryRegistryStore();
  await s.connect();
  await s.upsert(neuronRecord({ neuron_id: "a", capabilities: ["x"], version: "1.0" }));
  const got = await s.get("a");
  assert.equal(got?.neuron_id, "a");
  assert.deepEqual(got?.capabilities, ["x"]);
  assert.equal(got?.status, "registered");
});

test("upsert preserves the original registered_at", async () => {
  const s = new MemoryRegistryStore();
  await s.upsert(neuronRecord({ neuron_id: "a", registered_at: "2020-01-01T00:00:00.000Z" }));
  await s.upsert(neuronRecord({ neuron_id: "a", capabilities: ["new"] }));
  const got = await s.get("a");
  assert.equal(got?.registered_at, "2020-01-01T00:00:00.000Z");
  assert.deepEqual(got?.capabilities, ["new"]);
});

test("list excludes deregistered by default; filters by capability", async () => {
  const s = new MemoryRegistryStore();
  await s.upsert(neuronRecord({ neuron_id: "a", capabilities: ["chat"] }));
  await s.upsert(neuronRecord({ neuron_id: "b", capabilities: ["vision"] }));
  await s.markDeregistered("b");

  const live = await s.list();
  assert.deepEqual(live.map((r) => r.neuron_id), ["a"]);

  const all = await s.list({ includeDeregistered: true });
  assert.equal(all.length, 2);

  const chat = await s.list({ capability: "chat" });
  assert.deepEqual(chat.map((r) => r.neuron_id), ["a"]);
});

test("touchHeartbeat creates a thin record if REGISTER hasn't arrived", async () => {
  const s = new MemoryRegistryStore();
  await s.touchHeartbeat("ghost", "2026-05-20T00:00:00.000Z", "draining");
  const got = await s.get("ghost");
  assert.equal(got?.last_heartbeat, "2026-05-20T00:00:00.000Z");
  assert.equal(got?.status, "draining");
});

test("markDeregistered on an unknown neuron is a no-op", async () => {
  const s = new MemoryRegistryStore();
  await s.markDeregistered("nope"); // must not throw
  assert.equal(await s.get("nope"), null);
});
