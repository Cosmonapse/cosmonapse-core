import { useEffect, useRef, useState } from "react";
import type { CSSProperties } from "react";
import { brainSocketUrl } from "../api";
import { C, MONO } from "../theme";
import type { ReceptorCommand, ReceptorInfo } from "../types";
import { kindColor } from "./CanvasNode";

/** Python source defaults arrive quoted ("'world'"); a terminal line wants
 *  the bare value. */
const unquote = (s: string) => s.replace(/^['"]|['"]$/g, "");

/** SGR escape sequences. The REPL bolds its banner; nothing here renders
 *  colour, so the codes are stripped rather than half-interpreted. */
const ANSI = /\x1b\[[0-9;]*[a-zA-Z]/g;

/**
 * The CliReceptor panel: a real terminal on the brain's stdin/stdout.
 *
 * There is no protocol here and that is the point. `CliReceptor.repl()` is a
 * plain `input()` loop, so driving it is just moving bytes - what you type
 * goes to the process's stdin, what it writes comes back. You get the actual
 * REPL: its banner, its prompt, argparse errors, `:help`, `:quit`.
 *
 * This is also the one receptor a browser could never reach on its own. It
 * speaks no HTTP; only something sitting next to the process can drive it,
 * which is exactly what Genesis is.
 */
export function TerminalPanel({
  projectPath,
  receptor,
  running,
}: {
  projectPath: string;
  receptor: ReceptorInfo;
  running: boolean;
}) {
  const [lines, setLines] = useState("");
  const [live, setLive] = useState(false);
  const [input, setInput] = useState("");
  const [history, setHistory] = useState<string[]>([]);
  const [histAt, setHistAt] = useState(-1);
  const ws = useRef<WebSocket | null>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const field = useRef<HTMLInputElement>(null);

  // One socket per (project, running) - reconnect when the brain comes up so
  // pressing Run doesn't require re-connecting the panel by hand.
  useEffect(() => {
    if (!running) {
      setLive(false);
      return;
    }
    const sock = new WebSocket(brainSocketUrl(projectPath));
    ws.current = sock;
    sock.onopen = () => setLive(true);
    sock.onclose = () => setLive(false);
    sock.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data) as { type: string; text?: string };
        if (msg.text) setLines((prev) => prev + msg.text);
      } catch {
        /* ignore a frame we don't understand rather than breaking the view */
      }
    };
    return () => {
      sock.close();
      ws.current = null;
    };
  }, [projectPath, running]);

  // Pin to the bottom, the way a terminal does.
  useEffect(() => {
    const el = scroller.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lines]);

  function send(text: string) {
    const sock = ws.current;
    if (!sock || sock.readyState !== WebSocket.OPEN) return;
    // Echo locally: the child's stdin is a pipe, not a tty, so nothing else
    // will ever show what was typed.
    setLines((prev) => prev + text + "\n");
    sock.send(JSON.stringify({ type: "in", text: text + "\n" }));
    if (text.trim()) {
      setHistory((h) => [text, ...h.filter((x) => x !== text)].slice(0, 50));
    }
    setHistAt(-1);
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Enter") {
      send(input);
      setInput("");
      return;
    }
    // Shell-style history on the arrow keys.
    if (e.key === "ArrowUp") {
      e.preventDefault();
      const next = Math.min(histAt + 1, history.length - 1);
      if (next >= 0) {
        setHistAt(next);
        setInput(history[next]);
      }
      return;
    }
    if (e.key === "ArrowDown") {
      e.preventDefault();
      const next = histAt - 1;
      setHistAt(next);
      setInput(next < 0 ? "" : history[next]);
    }
  }

  function typeCommand(cmd: ReceptorCommand) {
    // Prefill the line rather than running it: the argument usually needs
    // filling in, and a command that fired on a single click would be a
    // surprise in a panel whose whole job is deliberate testing.
    //
    // Deliberately NOT argv syntax. The REPL is not argparse: _repl_kwargs
    // binds the entire rest of the line to the command's *first* parameter
    // and leaves the others at their defaults. Prefilling `--name Ada` would
    // pass the literal string "--name Ada" as the name - it fails silently
    // and reads like a bug in the user's Neuron.
    const first = cmd.params[0];
    const arg = first ? unquote(first.default) || `<${first.name}>` : "";
    setInput(`:${cmd.name}${arg ? " " + arg : ""}`);
    field.current?.focus();
  }

  const color = kindColor().receptor;

  return (
    <div style={{ flex: 1, display: "flex", minHeight: 0 }}>
      <div style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0 }}>
        <PanelHeader
          receptor={receptor}
          live={live}
          running={running}
          right={
            <button onClick={() => setLines("")} style={ghost}>
              clear
            </button>
          }
        />

        <div
          ref={scroller}
          onClick={() => field.current?.focus()}
          style={{
            flex: 1,
            overflowY: "auto",
            padding: "14px 16px",
            background: "var(--bg-well)",
            fontFamily: MONO,
            fontSize: 14.5,
            lineHeight: 1.65,
            color: C.text,
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
            cursor: "text",
          }}
        >
          {lines.replace(ANSI, "") || (
            <span style={{ color: C.textFaint, fontWeight: 600, }}>
              {running
                ? "Waiting for the brain's first output…"
                : "brain.py isn't running. Press Run above, and this terminal will attach itself."}
            </span>
          )}
        </div>

        <div
          style={{
            flexShrink: 0,
            display: "flex",
            alignItems: "center",
            gap: 8,
            padding: "9px 16px",
            borderTop: `1px solid ${C.border}`,
          }}
        >
          <span style={{ fontFamily: MONO, fontSize: 14.5, color }}>
            {String(receptor.config.prompt ?? "> ").trim()}
          </span>
          <input
            ref={field}
            value={input}
            disabled={!live}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder={live ? "a request, or :help" : "not connected"}
            style={{
              flex: 1,
              background: "transparent",
              border: "none",
              outline: "none",
              color: C.text,
              fontFamily: MONO,
              fontSize: 14.5,
            }}
          />
          <button onClick={() => send(":quit")} disabled={!live} style={ghost}>
            :quit
          </button>
        </div>
      </div>

      <CommandPalette commands={receptor.commands} onPick={typeCommand} />
    </div>
  );
}

/**
 * The declared commands, read off the source.
 *
 * This is the thing only Genesis can offer: `@RECEPTOR.command` decorates
 * ordinary functions, so the command set and every parameter's type and
 * default are visible in the file. A terminal alone would make you run
 * `:help` and read; here the surface is just listed.
 */
function CommandPalette({
  commands,
  onPick,
}: {
  commands: ReceptorCommand[];
  onPick: (c: ReceptorCommand) => void;
}) {
  const color = kindColor().receptor;
  if (commands.length === 0) return null;
  return (
    <div
      style={{
        width: 236,
        flexShrink: 0,
        borderLeft: `1px solid ${C.border}`,
        overflowY: "auto",
        padding: "12px 12px 24px",
      }}
    >
      <div
        style={{
          fontSize: 13,
          color: C.textFaint, fontWeight: 600,
          textTransform: "uppercase",
          letterSpacing: "0.08em",
          margin: "0 2px 9px",
        }}
      >
        Commands
      </div>
      <div style={{ fontSize: 11, color: C.textFaint, fontWeight: 600, lineHeight: 1.5, margin: "0 2px 10px" }}>
        The REPL binds the rest of the line to the first parameter — it is not
        argparse, so <code style={{ fontFamily: MONO }}>--flags</code> only work
        from argv.
      </div>
      {commands.map((c) => (
        <div
          key={c.name}
          onClick={() => onPick(c)}
          title="Click to type this command into the prompt"
          style={{
            border: `1px solid ${C.border}`,
            borderRadius: 8,
            padding: "8px 10px",
            marginBottom: 7,
            cursor: "pointer",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <span style={{ fontFamily: MONO, fontSize: 14, color }}>:{c.name}</span>
            {c.local && (
              <span style={tag(C.warn)} title="Answered in the Receptor - nothing crosses the bus.">
                local
              </span>
            )}
            {c.is_default && (
              <span style={tag(C.textFaint)} title="Runs when you type a bare request.">
                default
              </span>
            )}
          </div>
          {c.help && (
            <div style={{ fontSize: 13, color: C.textFaint, fontWeight: 600, marginTop: 4, lineHeight: 1.45 }}>
              {c.help}
            </div>
          )}
          {c.params.length > 0 && (
            <div style={{ marginTop: 6 }}>
              {c.params.map((p, i) => (
                <div key={p.name} style={{ fontSize: 13, fontFamily: MONO, color: C.textDim, fontWeight: 600, }}>
                  {p.name}
                  <span style={{ color: C.textFaint, fontWeight: 600, }}>
                    {p.annotation ? ` ${p.annotation}` : ""}
                    {p.default ? ` = ${p.default}` : p.required ? " · required" : ""}
                  </span>
                  {i === 0 && (
                    <span
                      style={{ color: C.warn, marginLeft: 5 }}
                      title="In the REPL the whole line after the command name is bound to this parameter. The --flag form only works from argv: python brain.py greet --name Ada"
                    >
                      {" "}← the line
                    </span>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

// ── shared panel chrome, used by all three panels ─────────────────────────

export function PanelHeader({
  receptor,
  live,
  running,
  right,
}: {
  receptor: ReceptorInfo;
  live: boolean;
  running: boolean;
  right?: React.ReactNode;
}) {
  const color = kindColor().receptor;
  return (
    <div
      style={{
        flexShrink: 0,
        display: "flex",
        alignItems: "center",
        gap: 10,
        padding: "10px 16px",
        borderBottom: `1px solid ${C.border}`,
      }}
    >
      <span style={{ fontFamily: MONO, fontSize: 14.5, color: C.text }}>{receptor.id}</span>
      <span style={{ fontFamily: MONO, fontSize: 13, color }}>{receptor.callee}</span>
      <span style={{ fontFamily: MONO, fontSize: 13, color: C.textFaint, fontWeight: 600, }}>
        {receptor.path}
      </span>
      <span
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 5,
          fontSize: 13,
          fontFamily: MONO,
          color: live ? C.ok : C.textFaint,
        }}
      >
        <span
          style={{
            width: 6,
            height: 6,
            borderRadius: "50%",
            background: live ? C.ok : C.textFaint,
          }}
        />
        {live ? "attached" : running ? "connecting…" : "brain stopped"}
      </span>
      <div style={{ marginLeft: "auto", display: "flex", gap: 7 }}>{right}</div>
    </div>
  );
}

export const ghost: CSSProperties = {
  padding: "5px 10px",
  borderRadius: 7,
  border: "1px solid var(--border)",
  background: "transparent",
  color: "var(--text-dim)",
  fontSize: 13,
  fontFamily: MONO,
  cursor: "pointer",
};

function tag(color: string): CSSProperties {
  return {
    fontSize: 12,
    fontFamily: MONO,
    color,
    border: `1px solid ${color}55`,
    borderRadius: 4,
    padding: "0 4px",
    textTransform: "uppercase",
    letterSpacing: "0.05em",
  };
}
