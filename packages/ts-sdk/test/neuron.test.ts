import assert from "node:assert/strict";
import { afterEach, beforeEach, test } from "node:test";

import {
  anthropicNeuron,
  neuron,
  openaiNeuron,
  type Json,
} from "../src/index.js";

type Captured = { url: string; init: RequestInit; body: unknown };

const realFetch = globalThis.fetch;
let calls: Captured[] = [];

/** Install a fetch stub that records the request and returns `response` as JSON. */
function stubFetch(response: unknown): void {
  calls = [];
  globalThis.fetch = (async (url: string | URL | Request, init?: RequestInit) => {
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ url: String(url), init: init ?? {}, body });
    return new Response(JSON.stringify(response), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof fetch;
}

beforeEach(() => {
  delete process.env["OPENAI_API_KEY"];
  delete process.env["ANTHROPIC_API_KEY"];
  delete process.env["GROQ_API_KEY"];
});

afterEach(() => {
  globalThis.fetch = realFetch;
});

test("openaiNeuron posts chat/completions and parses the choice", async () => {
  stubFetch({ choices: [{ message: { content: "hi there" } }] });
  const fn = openaiNeuron({ model: "gpt-4o-mini", apiKey: "sk-test", system: "be nice" });
  const out = (await fn({ prompt: "hello" }, [])) as { response: string; meta: Json };

  assert.equal(out.response, "hi there");
  assert.equal(calls.length, 1);
  assert.equal(calls[0]!.url, "https://api.openai.com/v1/chat/completions");
  const headers = new Headers(calls[0]!.init.headers);
  assert.equal(headers.get("Authorization"), "Bearer sk-test");
  const body = calls[0]!.body as { model: string; messages: Array<{ role: string; content: string }> };
  assert.equal(body.model, "gpt-4o-mini");
  assert.deepEqual(body.messages, [
    { role: "system", content: "be nice" },
    { role: "user", content: "hello" },
  ]);
});

test("openaiNeuron reads the key from OPENAI_API_KEY", async () => {
  process.env["OPENAI_API_KEY"] = "sk-env";
  stubFetch({ choices: [{ message: { content: "ok" } }] });
  const fn = openaiNeuron({ model: "gpt-4o" });
  await fn({ prompt: "x" }, []);
  assert.equal(new Headers(calls[0]!.init.headers).get("Authorization"), "Bearer sk-env");
});

test("openaiNeuron throws without an API key", () => {
  assert.throws(() => openaiNeuron({ model: "gpt-4o" }), /OPENAI_API_KEY/);
});

test("anthropicNeuron promotes system messages and concatenates text blocks", async () => {
  stubFetch({ content: [{ type: "text", text: "a" }, { type: "text", text: "b" }] });
  const fn = anthropicNeuron({ model: "claude-sonnet-4-6", apiKey: "ak-test" });
  const out = (await fn(
    { messages: [{ role: "system", content: "sys" }, { role: "user", content: "q" }] },
    [],
  )) as { response: string };

  assert.equal(out.response, "ab");
  assert.equal(calls[0]!.url, "https://api.anthropic.com/v1/messages");
  const headers = new Headers(calls[0]!.init.headers);
  assert.equal(headers.get("x-api-key"), "ak-test");
  assert.equal(headers.get("anthropic-version"), "2023-06-01");
  const body = calls[0]!.body as { system: string; max_tokens: number; messages: unknown[] };
  assert.equal(body.system, "sys");
  assert.equal(body.max_tokens, 1024);
  assert.deepEqual(body.messages, [{ role: "user", content: "q" }]);
});

test("anthropicNeuron throws without an API key", () => {
  assert.throws(() => anthropicNeuron({ model: "claude-sonnet-4-6" }), /ANTHROPIC_API_KEY/);
});

test("neuron('groq') targets the groq endpoint with chat API", async () => {
  stubFetch({ choices: [{ message: { content: "g" } }] });
  const fn = neuron("groq", { model: "llama-3.1-70b", apiKey: "gk-test" });
  const out = (await fn({ prompt: "hi" }, [])) as { response: string };

  assert.equal(out.response, "g");
  assert.equal(calls[0]!.url, "https://api.groq.com/openai/v1/chat/completions");
  assert.equal(new Headers(calls[0]!.init.headers).get("Authorization"), "Bearer gk-test");
});

test("neuron() routes openai and anthropic sources", async () => {
  stubFetch({ choices: [{ message: { content: "r" } }] });
  const fn = neuron("openai", { model: "gpt-4o", apiKey: "sk" });
  const out = (await fn({ prompt: "q" }, [])) as { response: string };
  assert.equal(out.response, "r");
  assert.equal(calls[0]!.url, "https://api.openai.com/v1/chat/completions");
});

test("neuron() rejects an unknown source", () => {
  // @ts-expect-error  -  exercising the runtime guard with an invalid source
  assert.throws(() => neuron("gemini", {}), /Unknown neuron source 'gemini'/);
});
