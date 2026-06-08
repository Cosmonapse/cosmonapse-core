import assert from "node:assert/strict";
import { test } from "node:test";

import { NatsSynapse, taskSignal } from "../src/index.js";

// These tests do not require a running NATS broker. They exercise the
// optional-dependency contract and the pre-connect guards. Behaviour over a
// real broker must be verified in an environment where `nats` is installed.

// The "missing dependency" path only exists when `nats` is NOT installed. When
// the optional dep is present (e.g. after `npm install` pulls optionalDependencies),
// connect() would attempt a real connection instead, so skip rather than assert.
let natsInstalled = false;
try {
  await import("nats");
  natsInstalled = true;
} catch {
  natsInstalled = false;
}

test(
  "connect() fails with a clear message when 'nats' isn't installed",
  { skip: natsInstalled ? "'nats' is installed in this environment" : false },
  async () => {
    const syn = new NatsSynapse({ url: "nats://127.0.0.1:4222" });
    await assert.rejects(() => syn.connect(), /requires the 'nats' package/);
  },
);

test("publish/subscribe/request before connect() throw", async () => {
  const syn = new NatsSynapse();
  await assert.rejects(() => syn.publish("s", taskSignal({ input: {} })), /before connect/);
  await assert.rejects(() => syn.subscribe("s", () => {}), /before connect/);
  await assert.rejects(
    () => syn.request("s", taskSignal({ input: {} })),
    /before connect/,
  );
});
