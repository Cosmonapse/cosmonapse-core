/**
 * @cosmonapse/sdk  -  tool-call standards
 *
 * Ported from `cosmonapse.effector.standards`. Recognisers for the *native*
 * tool-call dialects models are actually trained to emit. Teaching a hosted
 * model a bespoke convention invites drift; speaking its mother tongue does
 * not. The Axon declares which dialect its Neuron speaks (`toolStandard`) and
 * these parsers translate that dialect into the one normalised shape the rest
 * of the protocol understands - the model never learns Cosmonapse exists.
 *
 * Supported standards:
 *
 *   `hermes`   Nous/Hermes function-calling XML tags, the de-facto open model
 *              dialect (Qwen, Hermes, many fine-tunes):
 *                  <tool_call>
 *                  {"name": "read", "arguments": {"path": "hello.py"}}
 *                  </tool_call>
 *
 *   `claude`   Anthropic tool_use content block, as JSON in text:
 *                  {"type": "tool_use", "id": "toolu_01...",
 *                   "name": "read", "input": {"path": "hello.py"}}
 *
 *   `codex`    OpenAI function-calling JSON - `tool_calls` array, legacy
 *              `function_call`, a bare exact-keys `{"name", "arguments"}`
 *              object (or `{"name", "parameters"}` - Meta's documented Llama
 *              reply shape), or the Responses-API / schema-echo variant
 *              `{"type": "function"|"function_call", "name",
 *              "arguments"|"parameters"}` (hosted Llamas parrot the advertised
 *              schema wrapper - the `type` marker is the licence to accept
 *              `parameters`); string-encoded `arguments` are decoded.
 *
 * Every parser is pure and synchronous: it takes the model's text and returns
 * the normalised call `{ tool, args, callId }` on a match, or null to fall
 * through (so ordinary prose and ordinary JSON output never misfire).
 * Multiple tool calls in one reply: the first is taken - the
 * ONE-action-per-step contract is the Axon's to enforce, not the parser's.
 */

import type { Json } from "./envelope.js";

/** The normalised native tool call every standard's parser produces. */
export interface NativeToolCall {
  tool: string;
  args: Json;
  callId: string | null;
}

/** A parser takes the model's text reply and returns the normalised call or null. */
export type ToolCallParser = (text: string) => NativeToolCall | null;

const HERMES_TAG = /<tool_call>\s*(\{[\s\S]*?\})\s*<\/tool_call>/g;
const FENCE_BLOCK = /```(?:json)?\s*([\s\S]*?)```/g;

function loads(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function isObj(v: unknown): v is Json {
  return v !== null && typeof v === "object" && !Array.isArray(v);
}

/**
 * The first balanced JSON object in `text`, tolerating trailing junk - models
 * bolt comments onto their calls (`{...}}  # done`), which a strict
 * `JSON.parse` rejects wholesale. The TS counterpart to Python's
 * `JSONDecoder.raw_decode` scan: find a '{', scan to its balanced close
 * (string- and escape-aware), and try to parse exactly that slice.
 */
function firstObj(text: string): Json | null {
  let i = text.indexOf("{");
  while (i !== -1) {
    let depth = 0;
    let inStr = false;
    let esc = false;
    for (let j = i; j < text.length; j++) {
      const ch = text[j]!;
      if (esc) {
        esc = false;
        continue;
      }
      if (inStr) {
        if (ch === "\\") esc = true;
        else if (ch === '"') inStr = false;
        continue;
      }
      if (ch === '"') inStr = true;
      else if (ch === "{") depth++;
      else if (ch === "}") {
        depth--;
        if (depth === 0) {
          const obj = loads(text.slice(i, j + 1));
          if (isObj(obj)) return obj;
          break; // balanced but unparsable: try the next '{'
        }
      }
    }
    i = text.indexOf("{", i + 1);
  }
  return null;
}

/**
 * JSON objects worth inspecting, in reply order (first call wins): the whole
 * reply when it *starts* with an object (trailing junk tolerated), then the
 * first object inside each ``` fence (prose around fences is tolerated;
 * prose-embedded bare objects are NOT scanned - that is where ordinary output
 * would start misfiring).
 */
function jsonCandidates(text: string): Json[] {
  const out: Json[] = [];
  const t = text.trim();
  if (t.startsWith("{")) {
    const obj = firstObj(t);
    if (obj !== null) out.push(obj);
  }
  FENCE_BLOCK.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = FENCE_BLOCK.exec(text)) !== null) {
    const obj = firstObj(m[1]!);
    if (obj !== null) out.push(obj);
  }
  return out;
}

/** Normalise an arguments value: an object passes, a string-encoded JSON
 *  object is decoded (the codex wire shape), anything else fails. */
function normArgs(args: unknown): Json | null {
  if (isObj(args)) return args;
  if (typeof args === "string") {
    const decoded = loads(args);
    if (isObj(decoded)) return decoded;
  }
  return null;
}

function call(tool: unknown, args: unknown, callId: unknown = null): NativeToolCall | null {
  if (typeof tool !== "string" || !tool) return null;
  const norm = normArgs(args ?? {});
  if (norm === null) return null;
  return {
    tool,
    args: norm,
    callId: typeof callId === "string" && callId ? callId : null,
  };
}

// ---------------------------------------------------------------------------
// The three standards
// ---------------------------------------------------------------------------

/** Nous/Hermes `<tool_call>{"name", "arguments"}</tool_call>` tags. */
export function parseHermes(text: string): NativeToolCall | null {
  if (!text || !text.includes("<tool_call>")) return null;
  HERMES_TAG.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = HERMES_TAG.exec(text)) !== null) {
    const obj = loads(m[1]!);
    if (isObj(obj)) {
      const hit = call(obj["name"], obj["arguments"], obj["id"]);
      if (hit !== null) return hit;
    }
  }
  return null;
}

/** Anthropic `{"type": "tool_use", "name", "input"}` block. */
export function parseClaude(text: string): NativeToolCall | null {
  if (!text || !text.includes("tool_use")) return null;
  for (const obj of jsonCandidates(text)) {
    if (obj["type"] !== "tool_use") continue;
    const hit = call(obj["name"], obj["input"], obj["id"]);
    if (hit !== null) return hit;
  }
  return null;
}

/** OpenAI function-calling JSON: `tool_calls` array, legacy `function_call`,
 *  or a bare exact-keys `{"name", "arguments"}`. */
export function parseCodex(text: string): NativeToolCall | null {
  if (!text) return null;
  for (const obj of jsonCandidates(text)) {
    const calls = obj["tool_calls"];
    if (Array.isArray(calls)) {
      for (const entry of calls) {
        if (!isObj(entry)) continue;
        const fn = entry["function"];
        if (!isObj(fn)) continue;
        const hit = call(fn["name"], fn["arguments"], entry["id"]);
        if (hit !== null) return hit;
      }
      continue;
    }
    const fc = obj["function_call"];
    if (isObj(fc)) {
      const hit = call(fc["name"], fc["arguments"], obj["id"]);
      if (hit !== null) return hit;
      continue;
    }
    const keys = Object.keys(obj).sort().join(",");
    // Bare function-call object: exactly {"name", "arguments"} - the
    // exact-keys rule keeps ordinary JSON outputs from misfiring.
    if (keys === "arguments,name") {
      const hit = call(obj["name"], obj["arguments"]);
      if (hit !== null) return hit;
      continue;
    }
    // Meta's documented Llama JSON tool format replies with
    // {"name", "parameters"} - the args key is literally "parameters".
    // Same exact-keys guard: any extra key means it is not a call.
    if (keys === "name,parameters") {
      const hit = call(obj["name"], obj["parameters"]);
      if (hit !== null) return hit;
      continue;
    }
    // Self-marked variant: {"type": "function"|"function_call", "name",
    // "arguments"|"parameters"} - the shape Responses-API items use and the
    // one hosted models drift into by echoing the advertised schema wrapper.
    // The explicit type marker is what licenses accepting "parameters" as the
    // arguments key; without it, ordinary JSON carrying a "parameters" field
    // must never misfire. (A real schema wrapper nests under a "function" key
    // and has no top-level "name", so it cannot match here.)
    if (obj["type"] === "function" || obj["type"] === "function_call") {
      const fn = obj["function"];
      if (isObj(fn)) {
        // Schema-echo-with-args drift: the model replays the whole advertised
        // wrapper ({"type": "function", "function": {name, description,
        // parameters-SCHEMA}}) and bolts the real args on as "arguments".
        // Only a true "arguments" key matches - fn["parameters"] is the
        // SCHEMA, never the args, so a pure schema echo (no "arguments")
        // still returns null.
        const args = obj["arguments"] ?? fn["arguments"];
        if (args !== undefined && args !== null) {
          const hit = call(fn["name"], args, obj["id"] ?? obj["call_id"]);
          if (hit !== null) return hit;
        }
        continue;
      }
      const args = obj["arguments"] ?? obj["parameters"];
      const hit = call(obj["name"], args, obj["id"] ?? obj["call_id"]);
      if (hit !== null) return hit;
    }
  }
  return null;
}

/** The supported native dialects, keyed by standard name. */
export const TOOL_STANDARDS: Readonly<Record<string, ToolCallParser>> = {
  hermes: parseHermes,
  claude: parseClaude,
  codex: parseCodex,
};

/**
 * Run the `standard`'s parser over a Neuron's raw output. Accepts the
 * LLM-source shape `{ response: text }` or a plain string; anything else has
 * no text to parse. Returns the normalised call or null.
 */
export function extractToolCall(raw: unknown, standard: string): NativeToolCall | null {
  const parser = TOOL_STANDARDS[standard];
  if (!parser) return null;
  if (typeof raw === "string") return parser(raw);
  if (isObj(raw)) {
    const text = raw["response"];
    if (typeof text === "string") return parser(text);
  }
  return null;
}
