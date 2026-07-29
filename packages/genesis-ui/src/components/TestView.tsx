import { useCallback, useEffect, useMemo, useState } from "react";
import type { CSSProperties } from "react";
import {
  brainStatus,
  readReceptors,
  startBrain,
  stopBrain,
} from "../api";
import { C, MONO } from "../theme";
import type {
  BrainStatus,
  InitError,
  ReceptorInfo,
  ScaffoldResult,
} from "../types";
import { cup, kindColor } from "./CanvasNode";
import { TerminalPanel } from "./TerminalPanel";
import { ApiPanel } from "./ApiPanel";
import { ChatPanel } from "./ChatPanel";

/** How often the Run pill re-checks the process while it's up. */
const POLL_MS = 2000;

/**
 * The Test tab.
 *
 * Two halves that deliberately don't know about each other:
 *
 *   Run      owns the process. One `python -u brain.py` per project, started
 *            and stopped explicitly, because brain.py is the user's code and
 *            starting it is a decision rather than a side effect of clicking
 *            around.
 *   Connect  owns a conversation with one receptor inside whatever is already
 *            running. It never spawns anything.
 *
 * That split is what makes the tab honest: the receptor list comes from the
 * source and is there before you press Run, connecting can't accidentally
 * start a second brain, and stopping the brain leaves every panel exactly
 * where it was - simply unable to reach anything.
 */
export function TestView({ scaffold }: { scaffold: ScaffoldResult }) {
  const [receptors, setReceptors] = useState<ReceptorInfo[]>([]);
  const [hasBrain, setHasBrain] = useState(true);
  const [brain, setBrain] = useState<BrainStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [connected, setConnected] = useState<string | null>(null);

  const path = scaffold.path;

  useEffect(() => {
    readReceptors(path)
      .then((r) => {
        setReceptors(r.receptors);
        setHasBrain(r.has_brain);
      })
      .catch(() => setReceptors([]));
  }, [path]);

  const refresh = useCallback(() => {
    brainStatus(path).then(setBrain).catch(() => setBrain(null));
  }, [path]);

  useEffect(refresh, [refresh]);

  // Poll only while it's up: a stopped brain has nothing to report, and a
  // brain that dies on its own is exactly what we want to notice.
  useEffect(() => {
    if (!brain?.running) return;
    const t = setInterval(refresh, POLL_MS);
    return () => clearInterval(t);
  }, [brain?.running, refresh]);

  async function run(fn: () => Promise<BrainStatus>) {
    setBusy(true);
    setError(null);
    try {
      setBrain(await fn());
    } catch (e) {
      setError((e as InitError).error || "Couldn't do that.");
    } finally {
      setBusy(false);
    }
  }

  const running = !!brain?.running;
  const active = useMemo(
    () => receptors.find((r) => r.path === connected) ?? null,
    [receptors, connected],
  );

  return (
    <div style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" }}>
      <RunBar
        running={running}
        brain={brain}
        busy={busy}
        error={error}
        hasBrain={hasBrain}
        connected={connected !== null}
        onStart={() => run(() => startBrain(path))}
        onStop={() => run(() => stopBrain(path))}
        onDisconnect={() => setConnected(null)}
      />

      <div style={{ flex: 1, display: "flex", minHeight: 0 }}>
        <ReceptorList
          receptors={receptors}
          connected={connected}
          running={running}
          onConnect={setConnected}
        />

        <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column" }}>
          {!active && <Placeholder receptors={receptors} hasBrain={hasBrain} />}
          {active && active.shape === "cli" && (
            <TerminalPanel key={active.path} projectPath={path} receptor={active} running={running} />
          )}
          {active && active.shape === "api" && (
            <ApiPanel key={active.path} projectPath={path} receptor={active} running={running} />
          )}
          {active && active.shape === "chat" && (
            <ChatPanel key={active.path} projectPath={path} receptor={active} running={running} />
          )}
          {active && !["cli", "api", "chat"].includes(active.shape) && (
            <div style={{ padding: 24 }}>
              <div style={noteStyle}>
                {active.id} is built from a class defined in your project, so Genesis
                can't tell which transport it speaks and has no panel for it. The three
                SDK Receptors — CliReceptor, ApiReceptor and ChatReceptor — each get one.
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ── the Run bar ───────────────────────────────────────────────────────────

function RunBar({
  running,
  brain,
  busy,
  error,
  hasBrain,
  connected,
  onStart,
  onStop,
  onDisconnect,
}: {
  running: boolean;
  brain: BrainStatus | null;
  busy: boolean;
  error: string | null;
  hasBrain: boolean;
  connected: boolean;
  onStart: () => void;
  onStop: () => void;
  onDisconnect: () => void;
}) {
  // A brain that exited on its own is worth saying out loud - most often it's
  // a traceback in the terminal panel, and silence would just look broken.
  const died = !running && brain?.exit_code != null;
  return (
    <div
      style={{
        flexShrink: 0,
        display: "flex",
        alignItems: "center",
        gap: 12,
        padding: "11px 20px",
        borderBottom: `1px solid ${C.border}`,
        background: "var(--bg-rail)",
      }}
    >
      <button
        onClick={running ? onStop : onStart}
        disabled={busy || !hasBrain}
        style={runStyle(running, busy || !hasBrain)}
      >
        {busy ? "…" : running ? "Stop" : "Run"}
      </button>

      {/* Disconnect drops the UI's attachment - it closes the terminal socket
          and returns to the picker - without touching the process. Stop owns
          the process; this owns the connection. Keeping them apart means you
          can leave a receptor alone without tearing the brain down, and
          reconnect without restarting it. */}
      <button
        onClick={onDisconnect}
        disabled={!connected}
        title={
          connected
            ? "Close the panel and detach from this Receptor. Leaves brain.py running."
            : "Nothing is connected."
        }
        style={disconnectStyle(connected)}
      >
        Disconnect
      </button>

      <span style={{ fontFamily: MONO, fontSize: 14, color: C.text }}>brain.py</span>

      <span
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 6,
          fontFamily: MONO,
          fontSize: 13.5,
          color: running ? C.ok : died ? C.danger : C.textFaint,
        }}
      >
        <span
          style={{
            width: 7,
            height: 7,
            borderRadius: "50%",
            background: running ? C.ok : died ? C.danger : C.textFaint,
            boxShadow: running ? `0 0 7px ${C.ok}` : "none",
          }}
        />
        {running
          ? `running · pid ${brain?.pid} · ${fmtUptime(brain?.uptime_s ?? 0)}`
          : died
            ? `exited with code ${brain?.exit_code}`
            : "not running"}
      </span>

      {!hasBrain && (
        <span style={{ fontSize: 13.5, color: C.warn }}>
          no brain.py in this project — there's nothing to run
        </span>
      )}
      {error && <span style={{ fontSize: 13.5, color: C.accent3 }}>{error}</span>}

      <span
        style={{ marginLeft: "auto", fontSize: 13, color: C.textFaint, fontWeight: 600, maxWidth: 420 }}
        title="Receptors are read from your source, so the list below is there before you press Run. Connecting talks to whatever is already running — it never starts anything."
      >
        Run starts the process · Connect opens a panel onto it
      </span>
    </div>
  );
}

function fmtUptime(s: number): string {
  if (s < 60) return `${Math.round(s)}s`;
  const m = Math.floor(s / 60);
  return m < 60 ? `${m}m ${Math.round(s % 60)}s` : `${Math.floor(m / 60)}h ${m % 60}m`;
}

// ── the receptor list ─────────────────────────────────────────────────────

const SHAPE_LABEL: Record<string, string> = {
  cli: "CliReceptor",
  api: "ApiReceptor",
  chat: "ChatReceptor",
};

function ReceptorList({
  receptors,
  connected,
  running,
  onConnect,
}: {
  receptors: ReceptorInfo[];
  connected: string | null;
  running: boolean;
  onConnect: (path: string) => void;
}) {
  const color = kindColor().receptor;
  return (
    <div
      style={{
        width: 288,
        flexShrink: 0,
        borderRight: `1px solid ${C.border}`,
        background: "var(--bg-rail)",
        overflowY: "auto",
        padding: "14px 12px",
      }}
    >
      <div
        style={{
          fontSize: 13,
          color: C.textFaint, fontWeight: 600,
          textTransform: "uppercase",
          letterSpacing: "0.08em",
          margin: "0 4px 10px",
        }}
      >
        Receptors · {receptors.length}
      </div>

      {receptors.length === 0 && (
        <div style={{ ...noteStyle, margin: 4 }}>
          This project mounts no Receptors yet. Add one from the Canvas tab and it
          will appear here.
        </div>
      )}

      {receptors.map((r) => {
        const on = r.path === connected;
        return (
          <div
            key={r.path}
            style={{
              border: `1px solid ${on ? color + "66" : C.border}`,
              background: on ? color + "12" : "transparent",
              borderRadius: 10,
              padding: "10px 11px",
              marginBottom: 8,
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <svg width="14" height="14" viewBox="-10 -10 20 20" style={{ flexShrink: 0 }}>
                <path d={cup(8)} fill="none" stroke={color} strokeWidth="1.8" />
              </svg>
              <span
                style={{
                  fontFamily: MONO,
                  fontSize: 14.5,
                  color: C.text,
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                }}
              >
                {r.id}
              </span>
            </div>

            <div style={{ fontSize: 13, fontFamily: MONO, color, margin: "6px 0 2px" }}>
              {SHAPE_LABEL[r.shape] ?? r.callee ?? "Receptor"}
            </div>
            <div style={{ fontSize: 13, color: C.textFaint, fontWeight: 600, lineHeight: 1.5 }}>
              {r.shape === "cli"
                ? `${r.commands.length} command${r.commands.length === 1 ? "" : "s"} · stdin`
                : `${r.config.host}:${r.config.port}${r.config.path}`}
            </div>

            <button
              onClick={() => onConnect(r.path)}
              disabled={on}
              style={{
                marginTop: 9,
                width: "100%",
                padding: "6px 10px",
                borderRadius: 7,
                border: `1px solid ${on ? color + "55" : C.border}`,
                background: on ? color + "18" : "transparent",
                color: on ? color : C.textDim,
                fontSize: 13.5,
                fontFamily: MONO,
                cursor: on ? "default" : "pointer",
              }}
            >
              {on ? "connected" : "Connect"}
            </button>

            {!running && on && (
              <div style={{ fontSize: 13, color: C.warn, marginTop: 6, lineHeight: 1.45 }}>
                brain.py isn't running — press Run above.
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function Placeholder({
  receptors,
  hasBrain,
}: {
  receptors: ReceptorInfo[];
  hasBrain: boolean;
}) {
  return (
    <div
      style={{
        flex: 1,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: 40,
      }}
    >
      <div style={{ maxWidth: 460, textAlign: "center" }}>
        <svg width="46" height="46" viewBox="-10 -12 20 22" style={{ marginBottom: 14 }}>
          <path d="M -8,0 A 8,8 0 0 1 8,0" fill="none" stroke={kindColor().receptor} strokeWidth="1.1" strokeOpacity="0.35" />
          <path d={cup(6)} fill="none" stroke={kindColor().receptor} strokeWidth="1.4" />
        </svg>
        <div style={{ fontSize: 15, color: C.text, marginBottom: 8, fontFamily: MONO }}>
          {receptors.length === 0 ? "No Receptors to test" : "Pick a Receptor to connect"}
        </div>
        <div style={{ fontSize: 14, color: C.textFaint, fontWeight: 600, lineHeight: 1.6 }}>
          {receptors.length === 0
            ? "A Receptor is the edge a request arrives at. Add one from the Canvas tab — CLI, HTTP or chat — and it will show up here."
            : hasBrain
              ? "Connect opens a panel onto a receptor inside the running brain: a terminal for a CliReceptor, a request builder for an ApiReceptor, a chat window for a ChatReceptor."
              : "This project has no brain.py, so there's nothing to run these against."}
        </div>
      </div>
    </div>
  );
}

// ── shared bits ───────────────────────────────────────────────────────────

function runStyle(running: boolean, disabled: boolean): CSSProperties {
  return {
    padding: "6px 18px",
    borderRadius: 8,
    border: "none",
    background: disabled
      ? C.bgElev
      : running
        ? "transparent"
        : `linear-gradient(135deg, ${C.accent}, ${C.accent2})`,
    boxShadow: running ? `inset 0 0 0 1px ${C.danger}66` : "none",
    color: disabled ? C.textFaint : running ? C.danger : C.onPrimary,
    fontWeight: 600,
    fontSize: 14.5,
    fontFamily: MONO,
    cursor: disabled ? "default" : "pointer",
    minWidth: 74,
  };
}

const noteStyle: CSSProperties = {
  border: `1px solid ${C.border}`,
  borderRadius: 9,
  padding: "11px 13px",
  fontSize: 13.5,
  color: C.textDim, fontWeight: 600,
  lineHeight: 1.6,
  background: "var(--bg-well)",
};

function disconnectStyle(enabled: boolean): CSSProperties {
  return {
    padding: "6px 14px",
    borderRadius: 8,
    border: `1px solid ${enabled ? "var(--border-strong)" : "var(--border)"}`,
    background: "transparent",
    color: enabled ? C.textDim : C.textFaint,
    fontSize: 12,
    fontFamily: MONO,
    cursor: enabled ? "pointer" : "default",
  };
}
