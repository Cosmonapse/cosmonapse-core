import assert from "node:assert/strict";
import { test } from "node:test";

import {
  Dendrite,
  Engram,
  EngramBinding,
  EngramClient,
  EngramTimeout,
  InMemoryEngram,
  SignalType,
  MemorySynapse,
  imprintSignal,
  receipt,
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

// ---------------------------------------------------------------------------
// Engram.serve() - the decorator-native form
// ---------------------------------------------------------------------------

test("ServedEngram: first non-null recall answers, hit shapes normalised", async () => {
  const e = Engram.serve({ engramId: "notes" });
  const seen: string[] = [];
  e.onRecall((_q, ctx) => {
    seen.push("gate");
    return ctx.minConfidence === 0.9 ? [] : null;   // null falls through
  });
  e.onRecall((q) => [{ id: "a", entry: { text: (q as Record<string, unknown>)["text"] } }]);

  assert.equal(e.engramKind, "context");
  const hits = await e.recall({ text: "hi" });
  assert.deepEqual(seen, ["gate", "gate"].slice(0, 1));
  assert.equal(hits.length, 1);
  assert.equal(hits[0]!.id, "a");
  assert.equal(hits[0]!.score, 1.0);
  assert.deepEqual(hits[0]!.entry, { text: "hi" });

  // the gate answers (empty array is an answer, not a fall-through)
  const gated = await e.recall({ text: "hi" }, { minConfidence: 0.9 });
  assert.equal(gated.length, 0);
});

test("ServedEngram: no recall handler answers -> empty, a miss is not an error", async () => {
  const e = Engram.serve({ engramId: "notes" });
  e.onRecall(() => null);
  assert.deepEqual(await e.recall({}), []);
  assert.equal(await e.canServe({}), true);
});

test("ServedEngram: imprint returns id, receipt, or error; throw becomes error", async () => {
  const e = Engram.serve({ engramId: "notes" });

  // unhandled: no handler registered
  const none = await e.imprint("add", { content: "x" });
  assert.equal(none.ok, false);
  assert.match(none.error!, /unhandled imprint op 'add'/);

  e.onImprint((op) => (op === "delete" ? null : null));   // always falls through
  e.onImprint((op, entry, ctx) => {
    if (op === "delete") throw new Error("boom");
    if (ctx.mergeKey === "m") {
      return receipt("other", op, { id: "custom", version: 3 });
    }
    return `id-${(entry as Record<string, unknown>)["content"]}`;
  });

  const added = await e.imprint("add", { content: "x" });
  assert.equal(added.ok, true);
  assert.equal(added.id, "id-x");
  assert.equal(added.engramId, "notes");

  const custom = await e.imprint("upsert", { content: "y" }, { mergeKey: "m" });
  assert.equal(custom.engramId, "other");
  assert.equal(custom.id, "custom");
  assert.equal(custom.version, 3);

  const failed = await e.imprint("delete", { id: "z" });
  assert.equal(failed.ok, false);
  assert.match(failed.error!, /Error: boom/);
});

test("ServedEngram: serves() gate overrides canServe; lifecycle hooks fire", async () => {
  const e = Engram.serve({ engramId: "notes", engramKind: "vector", capabilities: ["vector"] });
  assert.equal(await e.canServe({}), false);            // no recall handler yet
  e.serves((q) => "vector" in (q as Record<string, unknown>));
  assert.equal(await e.canServe({ text: "a" }), false);
  assert.equal(await e.canServe({ vector: [0.1] }), true);

  let connected = 0;
  let refreshed = 0;
  e.onConnect((owner) => {
    assert.equal(owner, e);
    connected++;
  });
  e.onRefresh(() => {
    refreshed++;
  });
  await e.connect();
  await e.refresh();
  await e.close();
  assert.equal(connected, 1);
  assert.equal(refreshed, 1);
});

test("Dendrite hosts a served Engram: recall/imprint round-trip over the wire", async () => {
  const e = Engram.serve({ engramId: "served-notes" });
  e.onRecall(() => [{ id: "h1", entry: { content: "remembered" }, score: 0.5 }]);
  e.onImprint(() => "w1");

  const syn = new MemorySynapse();
  await syn.connect();
  const host = new Dendrite({ synapse: syn, dendriteId: "host", heartbeatMs: 0 });
  await host.attachEngram(e);
  const caller = new Dendrite({ synapse: syn, dendriteId: "caller", heartbeatMs: 0 });
  await host.start();
  await caller.start();
  try {
    const result = await caller.recall({
      engramId: "served-notes",
      query: { text: "anything" },
      deadlineMs: 2000,
    });
    assert.equal(result.hits.length, 1);
    assert.equal(result.hits[0]!.id, "h1");
    assert.equal(result.hits[0]!.score, 0.5);
    assert.deepEqual(result.engramIds, ["served-notes"]);

    const rec = await caller.imprint({
      engramId: "served-notes",
      op: "add",
      entry: { content: "new" },
      awaitAck: true,
      deadlineMs: 2000,
    });
    assert.ok(rec && rec.ok);
    assert.equal(rec.id, "w1");
    assert.equal(rec.engramId, "served-notes");
  } finally {
    await caller.stop();
    await host.stop();
    await syn.close();
  }
});
