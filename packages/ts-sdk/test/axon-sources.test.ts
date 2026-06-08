/**
 * Tests for the source-paired Axon factories, recognisers, and detects*
 * decorators -- the TS parity of tests/test_axon_sources.py.
 */

import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import {
  Axon,
  parseLlmIntents,
  parseMcpIntents,
  taskSignal,
  SignalType,
  type AxonOptions,
  type Json,
  type NeuronFn,
  type OutputParser,
  type Recogniser,
  type Signal,
} from "../src/index.js";

function makeTask(input: Json = { q: "hi" }): Signal {
  return taskSignal({ input, directed: { id: "answerer" } });
}

/** Axon over a fake neuron whose reply you can set per test. */
function axonWith(parser?: OutputParser): { axon: Axon; set: (r: unknown) => void } {
  let reply: unknown = {};
  const fn: NeuronFn = () => reply as Json;
  const opts: AxonOptions = { neuronId: "answerer", neuronFn: fn };
  if (parser) opts.outputParser = parser;
  return { axon: new Axon(opts), set: (r) => { reply = r; } };
}

// ---------------------------------------------------------------------------
// LLM recogniser
// ---------------------------------------------------------------------------

test("llm plain text passes through", () => {
  const raw = { response: "the capital of France is Paris", meta: { x: 1 } };
  assert.equal(parseLlmIntents(raw), raw);
});

test("llm prose with braces is not an intent", () => {
  const raw = { response: "use the dict {'a': 1} in code" };
  assert.equal(parseLlmIntents(raw), raw);
});

test("llm whole-string clarification intent", () => {
  const out = parseLlmIntents({ response: '{"cosmo":"clarification","question":"which region?"}' }) as Record<string, unknown>;
  assert.equal(out["__clarification__"], true);
  assert.equal(out["question"], "which region?");
});

test("llm fenced permission intent", () => {
  const raw = { response: 'Sure.\n```json\n{"cosmo":"permission","action":"delete","scope":"/db"}\n```' };
  const out = parseLlmIntents(raw) as Record<string, unknown>;
  assert.equal(out["__permission__"], true);
  assert.equal(out["action"], "delete");
  assert.equal(out["scope"], "/db");
});

test("llm error intent", () => {
  const out = parseLlmIntents({ response: '{"cosmo":"error","code":"REFUSED","message":"no"}' }) as Record<string, unknown>;
  assert.equal(out["__error__"], true);
  assert.equal(out["code"], "REFUSED");
});

test("llm output intent unwraps", () => {
  const out = parseLlmIntents({ response: '{"cosmo":"output","output":{"answer":42}}' });
  assert.deepEqual(out, { answer: 42 });
});

test("llm non-cosmo json stays plain output", () => {
  const raw = { response: '{"foo":"bar"}' };
  assert.equal(parseLlmIntents(raw), raw);
});

// ---------------------------------------------------------------------------
// MCP recogniser
// ---------------------------------------------------------------------------

test("mcp is_error becomes error marker", () => {
  const out = parseMcpIntents({ response: "boom", is_error: true }) as Record<string, unknown>;
  assert.equal(out["__error__"], true);
  assert.equal(out["code"], "MCP_TOOL_ERROR");
  assert.match(String(out["message"]), /boom/);
});

test("mcp ok result passes through", () => {
  const raw = { response: "ok", is_error: false, result: { files: 3 } };
  assert.equal(parseMcpIntents(raw), raw);
});

test("mcp can drive clarification", () => {
  const out = parseMcpIntents({ response: '{"cosmo":"clarification","question":"path?"}', is_error: false }) as Record<string, unknown>;
  assert.equal(out["__clarification__"], true);
});

// ---------------------------------------------------------------------------
// End-to-end through handleTask
// ---------------------------------------------------------------------------

test("handleTask plain output", async () => {
  const { axon, set } = axonWith(parseLlmIntents);
  set({ response: "Paris", meta: {} });
  const sig = await axon.handleTask(makeTask());
  assert.equal(sig.type, SignalType.AGENT_OUTPUT);
  assert.equal((sig.payload["output"] as Record<string, unknown>)["response"], "Paris");
});

test("handleTask clarification", async () => {
  const { axon, set } = axonWith(parseLlmIntents);
  set({ response: '{"cosmo":"clarification","question":"which?"}' });
  const sig = await axon.handleTask(makeTask());
  assert.equal(sig.type, SignalType.CLARIFICATION);
  assert.equal(sig.payload["question"], "which?");
});

test("handleTask permission", async () => {
  const { axon, set } = axonWith(parseLlmIntents);
  set({ response: '{"cosmo":"permission","action":"rm","scope":"/x"}' });
  const sig = await axon.handleTask(makeTask());
  assert.equal(sig.type, SignalType.PERMISSION);
  assert.equal(sig.payload["action"], "rm");
});

test("handleTask error marker (mcp is_error)", async () => {
  const { axon, set } = axonWith(parseMcpIntents);
  set({ response: "nope", is_error: true });
  const sig = await axon.handleTask(makeTask());
  assert.equal(sig.type, SignalType.ERROR);
  assert.equal(sig.payload["code"], "MCP_TOOL_ERROR");
});

test("handleTask without a parser is unchanged", async () => {
  const { axon, set } = axonWith();
  set({ response: '{"cosmo":"clarification","question":"x"}' });
  const sig = await axon.handleTask(makeTask());
  assert.equal(sig.type, SignalType.AGENT_OUTPUT); // marker text not recognised
});

// ---------------------------------------------------------------------------
// detects* decorators
// ---------------------------------------------------------------------------

function decoratedAxon(): { axon: Axon; set: (r: unknown) => void } {
  const { axon, set } = axonWith();
  axon.detectsClarification((raw) => {
    const t = String((raw as Record<string, unknown>)["response"]).trim();
    return t.startsWith("ASK:") ? { question: t.slice(4).trim() } : null;
  });
  axon.detectsPermission(async (raw) => {
    const t = String((raw as Record<string, unknown>)["response"]).trim();
    return t.startsWith("NEED:") ? { action: t.slice(5).trim() } : null;
  });
  axon.detectsOutput((raw) => ({ answer: String((raw as Record<string, unknown>)["response"]).trim() }));
  return { axon, set };
}

test("decorator clarification", async () => {
  const { axon, set } = decoratedAxon();
  set({ response: "ASK: which region?" });
  const sig = await axon.handleTask(makeTask());
  assert.equal(sig.type, SignalType.CLARIFICATION);
  assert.equal(sig.payload["question"], "which region?");
});

test("decorator permission with async detector", async () => {
  const { axon, set } = decoratedAxon();
  set({ response: "NEED: delete db" });
  const sig = await axon.handleTask(makeTask());
  assert.equal(sig.type, SignalType.PERMISSION);
  assert.equal(sig.payload["action"], "delete db");
});

test("decorator output reshape", async () => {
  const { axon, set } = decoratedAxon();
  set({ response: "Paris" });
  const sig = await axon.handleTask(makeTask());
  assert.equal(sig.type, SignalType.AGENT_OUTPUT);
  assert.deepEqual(sig.payload["output"], { answer: "Paris" });
});

test("decorator error precedence", async () => {
  const { axon, set } = decoratedAxon();
  axon.detectsError((raw) =>
    String((raw as Record<string, unknown>)["response"]).includes("boom")
      ? { code: "X", message: "m" }
      : null,
  );
  set({ response: "ASK: q boom" }); // both error and clarification match
  const sig = await axon.handleTask(makeTask());
  assert.equal(sig.type, SignalType.ERROR); // error wins
  assert.equal(sig.payload["code"], "X");
});

// ---------------------------------------------------------------------------
// Factory wiring
// ---------------------------------------------------------------------------

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
  delete process.env["OPENAI_API_KEY"];
});

test("Axon.openai constructs and is an Axon", () => {
  const axon = Axon.openai("writer", { model: "gpt-4o-mini", apiKey: "sk-test" }, { capabilities: ["writing"] });
  assert.ok(axon instanceof Axon);
  assert.equal(axon.neuronId, "writer");
  assert.deepEqual(axon.capabilities, ["writing"]);
});

test("Axon.openai recognises a cosmo clarification end-to-end", async () => {
  // Stub fetch: the model emits a cosmo clarification block as its content.
  globalThis.fetch = (async () =>
    new Response(
      JSON.stringify({ choices: [{ message: { content: '{"cosmo":"clarification","question":"region?"}' } }] }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    )) as typeof fetch;

  const axon = Axon.openai("writer", { model: "gpt-4o-mini", apiKey: "sk-test" });
  const sig = await axon.handleTask(makeTask({ prompt: "hi" }));
  assert.equal(sig.type, SignalType.CLARIFICATION);
  assert.equal(sig.payload["question"], "region?");
});

test("Axon.openai with recognize:false leaves output verbatim", async () => {
  globalThis.fetch = (async () =>
    new Response(
      JSON.stringify({ choices: [{ message: { content: '{"cosmo":"clarification","question":"x"}' } }] }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    )) as typeof fetch;

  const axon = Axon.openai("writer", { model: "gpt-4o-mini", apiKey: "sk-test" }, { recognize: false });
  const sig = await axon.handleTask(makeTask({ prompt: "hi" }));
  assert.equal(sig.type, SignalType.AGENT_OUTPUT); // not recognised
});
