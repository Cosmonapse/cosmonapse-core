/**
 * @cosmonapse/sdk — MCP-server Neuron
 *
 * Wrap **any stdio MCP server** as a Neuron. This is a thin client wrapper —
 * it does NOT implement an MCP server. It spawns an existing server as a
 * subprocess, speaks MCP over stdio via `@modelcontextprotocol/sdk`, and
 * exposes that server's tools behind the NeuronFn signature. A TASK becomes an
 * MCP `tools/call`; the tool result becomes the Neuron output.
 *
 * The `@modelcontextprotocol/sdk` package is an optional peer dependency and is
 * imported lazily, so projects that don't use MCP neurons don't need it.
 *
 * ```ts
 * import { Axon, mcpNeuron } from "@cosmonapse/sdk";
 *
 * const files = mcpNeuron({ server: "filesystem", args: ["/data"], tool: "read_file" });
 * const axon = new Axon({ neuronId: "files", neuronFn: files });
 * ```
 *
 * Input dict:
 *   tool              Tool to call (falls back to `opts.tool`, or the sole tool).
 *   arguments | args  Tool arguments (object). If omitted, all non-control keys
 *                     are used as arguments.
 *   __list_tools__    When truthy, return the server's tool catalogue.
 *
 * Output (`tools/call`):
 *   { response, result, is_error, content, meta:{tool,server,command} }
 * Output (`__list_tools__`):
 *   { tools: [{ name, description, input_schema }] }
 */

import type { Json } from "./envelope.js";
import type { CloseableNeuronFn } from "./neuron-express.js";

export interface McpNeuronOptions {
  /** Executable to spawn (e.g. "npx", "uvx"). Required unless `server` is set. */
  command?: string;
  /** Arguments. Appended after a preset's args when `server` is set. */
  args?: string[];
  /** Name of a standard server preset (see {@link standardMcpServers}). */
  server?: string;
  /** Extra environment variables for the subprocess. */
  env?: Record<string, string>;
  /** Working directory for the subprocess. */
  cwd?: string;
  /** Default tool name used when the input omits `tool`. */
  tool?: string;
  clientName?: string;
  clientVersion?: string;
}

/**
 * Launch specs for standard, separately-published MCP servers. We wrap them —
 * we do not ship them. Anything in `opts.args` is appended (e.g. allowed dirs
 * for filesystem, `--repository <path>` for git).
 */
export const standardMcpServers: Record<string, { command: string; args: string[]; note: string }> = {
  filesystem: {
    command: "npx",
    args: ["-y", "@modelcontextprotocol/server-filesystem"],
    note: "Append one or more allowed directories, e.g. args=['/data'].",
  },
  memory: {
    command: "npx",
    args: ["-y", "@modelcontextprotocol/server-memory"],
    note: "Knowledge-graph memory store.",
  },
  everything: {
    command: "npx",
    args: ["-y", "@modelcontextprotocol/server-everything"],
    note: "Reference server exercising every MCP feature; handy for tests.",
  },
  sequentialthinking: {
    command: "npx",
    args: ["-y", "@modelcontextprotocol/server-sequential-thinking"],
    note: "Structured step-by-step reasoning tool.",
  },
  fetch: {
    command: "uvx",
    args: ["mcp-server-fetch"],
    note: "Fetch a URL and return its content as markdown/text.",
  },
  git: {
    command: "uvx",
    args: ["mcp-server-git"],
    note: "Read/inspect a git repo. Append --repository <path>.",
  },
  time: {
    command: "uvx",
    args: ["mcp-server-time"],
    note: "Current time and timezone conversions.",
  },
};

const CONTROL_KEYS = new Set(["tool", "arguments", "args", "__list_tools__"]);

function resolveLaunch(opts: McpNeuronOptions): { command: string; args: string[] } {
  const extra = opts.args ?? [];
  if (opts.server != null) {
    const preset = standardMcpServers[opts.server];
    if (!preset) {
      const available = Object.keys(standardMcpServers).sort().join(", ");
      throw new Error(
        `Unknown MCP server preset '${opts.server}'. Available: ${available}. ` +
          `(Or pass command/args to wrap any other stdio MCP server.)`,
      );
    }
    return { command: opts.command ?? preset.command, args: [...preset.args, ...extra] };
  }
  if (opts.command == null) {
    throw new Error("mcpNeuron(...) needs either `command` (+optional `args`) or a `server` preset name.");
  }
  return { command: opts.command, args: extra };
}

interface McpClient {
  connect(transport: unknown): Promise<void>;
  listTools(): Promise<{ tools?: Array<{ name: string; description?: string; inputSchema?: unknown }> }>;
  callTool(req: { name: string; arguments: Record<string, unknown> }): Promise<{
    content?: Array<{ type?: string; text?: string }>;
    structuredContent?: unknown;
    isError?: boolean;
  }>;
  close(): Promise<void>;
}

export function mcpNeuron(opts: McpNeuronOptions): CloseableNeuronFn {
  const { command, args } = resolveLaunch(opts);
  let client: McpClient | null = null;
  let connecting: Promise<McpClient> | null = null;

  async function ensure(): Promise<McpClient> {
    if (client) return client;
    if (connecting) return connecting;
    connecting = (async () => {
      // Lazy, optional dependency — imported only when an MCP neuron runs.
      // Variable specifiers keep the import non-analyzable, so `tsc`/bundlers
      // don't require @modelcontextprotocol/sdk to be installed at build time.
      const clientSpec = "@modelcontextprotocol/sdk/client/index.js";
      const stdioSpec = "@modelcontextprotocol/sdk/client/stdio.js";
      const clientMod = (await import(clientSpec)) as Record<string, unknown>;
      const stdioMod = (await import(stdioSpec)) as Record<string, unknown>;
      const Client = (clientMod as { Client: new (info: unknown, opts: unknown) => McpClient }).Client;
      const StdioClientTransport = (stdioMod as {
        StdioClientTransport: new (params: unknown) => unknown;
      }).StdioClientTransport;

      const transport = new StdioClientTransport({
        command,
        args,
        ...(opts.env ? { env: opts.env } : {}),
        ...(opts.cwd ? { cwd: opts.cwd } : {}),
      });
      const c = new Client(
        { name: opts.clientName ?? "cosmonapse", version: opts.clientVersion ?? "0.2.0" },
        { capabilities: {} },
      );
      await c.connect(transport);
      client = c;
      return c;
    })();
    return connecting;
  }

  const fn = (async (input: Json, _context: unknown[]): Promise<Json> => {
    const c = await ensure();
    const inp = (input ?? {}) as Record<string, unknown>;

    if (inp.__list_tools__) {
      const res = await c.listTools();
      return {
        tools: (res.tools ?? []).map((t) => ({
          name: t.name,
          description: t.description ?? null,
          input_schema: (t.inputSchema as Json) ?? null,
        })),
      } as Json;
    }

    let tool = (inp.tool as string | undefined) ?? opts.tool;
    let toolArgs = (inp.arguments ?? inp.args) as Record<string, unknown> | undefined;
    if (toolArgs == null) {
      toolArgs = {};
      for (const [k, v] of Object.entries(inp)) {
        if (!CONTROL_KEYS.has(k)) toolArgs[k] = v;
      }
    }

    if (!tool) {
      const res = await c.listTools();
      const names = (res.tools ?? []).map((t) => t.name);
      if (names.length === 1) {
        tool = names[0];
      } else {
        throw new Error(
          `MCP Neuron could not determine which tool to call. Pass tool=... (server exposes: ${JSON.stringify(names)}).`,
        );
      }
    }

    const res = await c.callTool({ name: tool, arguments: toolArgs });
    const content = res.content ?? [];
    const texts = content.filter((x) => x?.text != null).map((x) => x.text as string);

    return {
      response: texts.join("\n"),
      result: (res.structuredContent as Json) ?? null,
      is_error: Boolean(res.isError),
      content: content as Json,
      meta: { tool, server: opts.server ?? null, command },
    } as Json;
  }) as CloseableNeuronFn;

  fn.close = async (): Promise<void> => {
    const c = client;
    client = null;
    connecting = null;
    if (c) {
      try {
        await c.close();
      } catch {
        /* teardown must not throw */
      }
    }
  };

  return fn;
}
