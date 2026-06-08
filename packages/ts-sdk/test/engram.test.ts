import assert from "node:assert/strict";
import { test } from "node:test";

import {
  EngramBinding,
  EngramClient,
  EngramTimeout,
  InMemoryEngram,
  SignalType,
  imprintSignal,
  recallSignal,
  recalledSignal,
  type EngramPublisher,
  type Signal,
} from "../src/index.js";

test("InMemoryEngram add + recall by text/tag", async () => {
  const e = new InMemoryEngram();
  await e.connect();
  const r = await e.imprint("add", { content: "hello world", tags: ["greet"] });
  assert.ok(r.ok);
  assert.ok(r.id);
  const byText = await e.recall({ text: "hello" });
  assert.equal(byText.length, 1);
  const byTag = await e.recall({}, { filters: { tags: ["greet"] } });
  assert.equal(byTag.length, 1);
  const miss = await e.recall({ text: "nope" });
  assert.equal(miss.length, 0);
});

test("InMemoryEngram upsert + merge bump version", async () => {
  const e = new InMemoryEngram();
  const a = await e.imprint("upsert", { content: { a: 1 } }, { mergeKey: "k" });
  assert.equal(a.version, 1);
  const b = await e.imprint("upsert", { content: { a: 2 } }, { mergeKey: "k" });
  assert.equal(b.version, 2);
  assert.equal(b.id, a.id);
  const m = await e.imprint("merge", { content: { c: 3 } }, { mergeKey: "k" });
  assert.equal(m.version, 3);
});

test("InMemoryEngram imprint idempotency by imprintId", async () => {
  const e = new InMemoryEngram();
  const r1 = await e.imprint("append", { content: "x" }, { imprintId: "imp1" });
  const r2 = await e.imprint("append", { content: "x" }, { imprintId: "imp1" });
  assert.equal(r1.id, r2.id);
  assert.equal(e.snapshot().length, 1);
});

test("recall/imprint signal builders validate addressing", () => {
  assert.throws(() => recallSignal({ traceId: "trc_x", parentId: "evt_p", directed: null, query: {} }));
  const ok = recallSignal({ traceId: "trc_x", parentId: "evt_p", directed: { id: "eng-1" }, query: { text: "q" } });
  assert.equal(ok.type, SignalType.RECALL);
  assert.equal(ok.payload["recall_mode"], "first");
  assert.throws(() =>
    imprintSignal({ traceId: "trc_x", parentId: "evt_p", directed: { id: "e" }, op: "merge", entry: {} }),
  );
});

test("EngramBinding requires an address", () => {
  assert.throws(() => new EngramBinding({ name: "ctx" }));
  const b = new EngramBinding({ name: "ctx", directedType: "context" });
  assert.equal(b.toDirected().type, "context");
});

test("EngramClient correlates RECALLED by parent_id", async () => {
  const sent: Signal[] = [];
  const pub: EngramPublisher = {
    publish: async (s) => {
      sent.push(s);
    },
  };
  const client = new EngramClient(pub);
  const p = client.recall({ engramId: "e1", query: { text: "q" }, traceId: "trc_x", parentId: "evt_t" });
  const req = sent[0]!;
  client.deliver(
    recalledSignal({
      traceId: "trc_x",
      parentId: req.id,
      engramId: "e1",
      hits: [{ id: "h1", entry: { v: 1 }, score: 1 }],
    }),
  );
  const res = await p;
  assert.equal(res.hits.length, 1);
  assert.deepEqual(res.engramIds, ["e1"]);
});

test("EngramClient recall times out without a responder", async () => {
  const pub: EngramPublisher = { publish: async () => {} };
  const client = new EngramClient(pub);
  await assert.rejects(
    client.recall({ engramId: "e1", query: {}, deadlineMs: 10, traceId: "trc_x", parentId: "evt_t" }),
    EngramTimeout,
  );
});
