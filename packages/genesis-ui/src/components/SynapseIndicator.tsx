import { useCallback, useEffect, useRef, useState } from "react";
import type { CSSProperties, ReactNode } from "react";
import { launchPrism, startSynapse, stopSynapse, synapseStatus } from "../api";
import { loadSynapseUrl, rememberSynapseUrl } from "../recents";
import { C, MONO } from "../theme";
import type { InitError, SynapseStatus, SynapseTransport } from "../types";

const DEFAULT_SYNAPSE_PORT = 7070;
const DEFAULT_PRISM_PORT = 7071;
/** Slow on purpose: this is a liveness light, not a metrics feed. */
const POLL_MS = 5000;

/**
 * Whether this brain has a synapse behind it, and the one place to change
 * the answer.
 *
 * A project on disk is only half a running system - the other half is a
 * broker on a port, in another process, that Genesis can't infer. So the
 * indicator states the truth it can actually check (does something answer
 * for this namespace?) and, when the answer is no, opens the form that
 * makes it yes. Live, it becomes the way into Prism, because once signals
 * are flowing the next thing you want is to watch them.
 *
 * The namespace is never editable here. config.py decided it at scaffold
 * time; a field you could retype would only be a way to connect to the
 * wrong one.
 */
export function SynapseIndicator({
  projectPath,
  namespace,
}: {
  projectPath: string;
  namespace: string | null;
}) {
  const [url, setUrl] = useState(() => loadSynapseUrl(projectPath));
  const [status, setStatus] = useState<SynapseStatus | null>(null);
  const [open, setOpen] = useState(false);
  const wrap = useRef<HTMLDivElement>(null);

  const refresh = useCallback(async () => {
    if (!namespace || !url) {
      setStatus(null);
      return;
    }
    try {
      setStatus(await synapseStatus(url, namespace));
    } catch {
      setStatus(null);
    }
  }, [url, namespace]);

  // Re-probe on a timer as well as on open: a synapse can go down without
  // anyone touching Genesis, and a light that only updates when clicked is
  // worse than no light.
  useEffect(() => {
    refresh();
    const t = setInterval(refresh, POLL_MS);
    return () => clearInterval(t);
  }, [refresh]);

  useEffect(() => {
    setUrl(loadSynapseUrl(projectPath));
  }, [projectPath]);

  useEffect(() => {
    if (!open) return;
    function onPointer(e: PointerEvent) {
      if (!wrap.current?.contains(e.target as Node)) setOpen(false);
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("pointerdown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  function adopt(next: SynapseStatus) {
    setStatus(next);
    setUrl(next.url);
    rememberSynapseUrl(projectPath, next.url);
  }

  const live = !!status?.live;

  return (
    <div ref={wrap} style={{ position: "relative", flexShrink: 0 }}>
      <button
        onClick={() => setOpen((v) => !v)}
        title={
          live
            ? `Live on ${status?.url} · ${namespace}`
            : "No synapse - click to start one"
        }
        style={pillStyle(live, open)}
      >
        <Dot live={live} />
        <span>{live ? "synapse live" : "no synapse"}</span>
        {namespace && (
          <span style={{ color: C.textFaint, fontWeight: 600, }}>· {namespace}</span>
        )}
      </button>

      {open && (
        <div style={panelStyle}>
          {live && status ? (
            <LivePanel
              status={status}
              onStopped={(s) => setStatus(s)}
              onRefresh={refresh}
            />
          ) : (
            <StartPanel
              namespace={namespace}
              lastUrl={url}
              status={status}
              onStarted={adopt}
              onAttached={adopt}
            />
          )}
        </div>
      )}
    </div>
  );
}

/* ─────────────────────────  not live: choose one  ───────────────────────── */

const TRANSPORTS: {
  id: SynapseTransport;
  label: string;
  blurb: string;
  enabled: boolean;
}[] = [
  {
    id: "memory",
    label: "Memory",
    blurb: "in-process, no server",
    enabled: true,
  },
  {
    id: "dev",
    label: "DevSynapse",
    blurb: "TCP on a port you pick",
    enabled: true,
  },
  { id: "nats", label: "NATS", blurb: "bring your own broker", enabled: false },
  { id: "kafka", label: "Kafka", blurb: "bring your own broker", enabled: false },
];

function StartPanel({
  namespace,
  lastUrl,
  status,
  onStarted,
  onAttached,
}: {
  namespace: string | null;
  /** The URL this project was last pointed at - seeds the port field. */
  lastUrl: string;
  status: SynapseStatus | null;
  onStarted: (s: SynapseStatus) => void;
  onAttached: (s: SynapseStatus) => void;
}) {
  const [pick, setPick] = useState<SynapseTransport>("dev");
  // Offer the port you used last for this project rather than the global
  // default: after a stop, restarting on the same port is the common case.
  const [port, setPort] = useState(() => portOf(lastUrl) || String(DEFAULT_SYNAPSE_PORT));
  const [attach, setAttach] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!namespace) {
    return (
      <>
        <Title>Synapse</Title>
        <div style={noteStyle}>
          This project has no <code style={codeStyle}>NAMESPACE</code> in
          config.py, so there's nothing for a synapse to serve. Add one and
          reload.
        </div>
      </>
    );
  }

  async function start() {
    setBusy(true);
    setError(null);
    try {
      onStarted(
        await startSynapse({
          namespace: namespace!,
          port: Number(port) || DEFAULT_SYNAPSE_PORT,
        }),
      );
    } catch (e) {
      setError((e as InitError).error || "Couldn't start the synapse.");
    } finally {
      setBusy(false);
    }
  }

  async function attachTo() {
    setBusy(true);
    setError(null);
    try {
      const s = await synapseStatus(attach.trim(), namespace!);
      if (!s.live) setError(s.reason || "Nothing is serving that namespace.");
      else onAttached(s);
    } catch {
      setError("Couldn't reach that URL.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <Title>Open a synapse</Title>
      <div style={{ ...noteStyle, marginBottom: 12 }}>
        Nothing is serving <code style={codeStyle}>{namespace}</code> yet.
        {status?.reason ? ` ${status.reason}` : ""}
      </div>

      <div style={{ display: "flex", gap: 6, marginBottom: 12 }}>
        {TRANSPORTS.map((t) => {
          const on = t.id === pick;
          return (
            <button
              key={t.id}
              disabled={!t.enabled}
              onClick={() => t.enabled && setPick(t.id)}
              title={t.enabled ? t.blurb : `${t.blurb} - not startable from Genesis`}
              style={{
                flex: 1,
                padding: "7px 4px",
                borderRadius: 8,
                fontFamily: MONO,
                fontSize: 13.5,
                cursor: t.enabled ? "pointer" : "not-allowed",
                opacity: t.enabled ? 1 : 0.38,
                color: on ? C.accent2 : C.textDim,
                background: on ? "rgba(var(--accent2-rgb), 0.12)" : "transparent",
                border: "1px solid " + (on ? "rgba(var(--accent2-rgb), 0.4)" : C.border),
                transition: "all 0.15s",
              }}
            >
              {t.label}
            </button>
          );
        })}
      </div>

      {pick === "memory" && (
        <>
          <div style={warnStyle}>
            An in-process MemorySynapse has no server and no port, so nothing
            outside <code style={codeStyle}>brain.py</code> can subscribe to it —
            Prism will stay dark and this indicator can't turn green.
            Observability is minimal by construction. Run{" "}
            <code style={codeStyle}>python brain.py</code> with{" "}
            <code style={codeStyle}>SYNAPSE_URL</code> unset and it's already what
            you get.
          </div>
          <div style={{ ...noteStyle, marginTop: 10 }}>
            Pick DevSynapse instead when you want to watch signals.
          </div>
        </>
      )}

      {pick === "dev" && (
        <>
          <Row>
            <Labelled label="Namespace" flex={1.3}>
              {/* Locked: config.py already decided this. */}
              <input
                value={namespace}
                readOnly
                disabled
                title="Set by config.py - edit it there, not here"
                style={{ ...inputStyle, opacity: 0.62, cursor: "not-allowed" }}
              />
            </Labelled>
            <Labelled label="Port" flex={1}>
              <input
                value={port}
                onChange={(e) => setPort(e.target.value.replace(/[^0-9]/g, ""))}
                placeholder={String(DEFAULT_SYNAPSE_PORT)}
                style={inputStyle}
              />
            </Labelled>
          </Row>
          <div style={{ ...noteStyle, margin: "2px 0 12px" }}>
            Starts <code style={codeStyle}>cosmo synapse start memory</code> on{" "}
            <code style={codeStyle}>cosmo://127.0.0.1:{port || DEFAULT_SYNAPSE_PORT}</code>.
            One port per project — pick another if this one's taken.
          </div>
          {error && <div style={errorStyle}>{error}</div>}
          <button onClick={start} disabled={busy} style={primaryStyle(!busy)}>
            {busy ? "Starting…" : "Start"}
          </button>
        </>
      )}

      {(pick === "nats" || pick === "kafka") && (
        <div style={noteStyle}>
          Genesis doesn't manage brokers it didn't start. Run one yourself, then
          attach to it below.
        </div>
      )}

      <div style={dividerStyle} />
      <Labelled label="Or attach to a running synapse">
        <div style={{ display: "flex", gap: 6 }}>
          <input
            value={attach}
            onChange={(e) => setAttach(e.target.value)}
            placeholder="cosmo://127.0.0.1:7070"
            style={inputStyle}
          />
          <button
            onClick={attachTo}
            disabled={busy || !attach.trim()}
            style={{ ...secondaryStyle, flexShrink: 0 }}
          >
            attach
          </button>
        </div>
      </Labelled>
    </>
  );
}

/* ───────────────────────────  live: watch it  ─────────────────────────── */

function LivePanel({
  status,
  onStopped,
  onRefresh,
}: {
  status: SynapseStatus;
  onStopped: (s: SynapseStatus) => void;
  onRefresh: () => void;
}) {
  const [port, setPort] = useState(String(DEFAULT_PRISM_PORT));
  const [busy, setBusy] = useState<"prism" | "stop" | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function openPrism() {
    setBusy("prism");
    setError(null);
    try {
      const launched = await launchPrism({
        url: status.url,
        namespace: status.namespace,
        port: Number(port) || DEFAULT_PRISM_PORT,
      });
      window.open(launched.url, "_blank", "noopener");
    } catch (e) {
      setError((e as InitError).error || "Couldn't launch Prism.");
    } finally {
      setBusy(null);
    }
  }

  async function stop() {
    setBusy("stop");
    setError(null);
    try {
      onStopped(await stopSynapse(status.url, status.namespace));
    } catch (e) {
      setError((e as InitError).error || "Couldn't stop the synapse.");
    } finally {
      setBusy(null);
    }
  }

  return (
    <>
      <Title>
        Synapse live
        <span style={{ marginLeft: "auto", ...ghostStyle }} onClick={onRefresh}>
          refresh
        </span>
      </Title>

      <div style={factsStyle}>
        <Fact k="url" v={status.url} />
        <Fact k="namespace" v={status.namespace} />
        <Fact k="transport" v={status.transport ?? "?"} />
        <Fact k="signals" v={String(status.signal_count ?? 0)} />
        {status.client_count != null && (
          <Fact k="clients" v={String(status.client_count)} />
        )}
      </div>

      <div style={{ ...noteStyle, margin: "10px 0 12px" }}>
        Run the brain against it with{" "}
        <code style={codeStyle}>SYNAPSE_URL={status.url} python brain.py</code>.
      </div>

      <div style={dividerStyle} />

      <Labelled label="Launch Prism">
        <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <input
            value={port}
            onChange={(e) => setPort(e.target.value.replace(/[^0-9]/g, ""))}
            placeholder={String(DEFAULT_PRISM_PORT)}
            title="Port for the Prism server"
            style={{ ...inputStyle, width: 78, flex: "0 0 78px" }}
          />
          <button
            onClick={openPrism}
            disabled={busy !== null}
            style={{ ...primaryStyle(busy === null), flex: 1, marginTop: 0 }}
          >
            {busy === "prism" ? "Launching…" : "Launch Prism"}
          </button>
        </div>
      </Labelled>
      <div style={noteStyle}>
        Opens Prism pointed at this synapse and namespace. Reuses whatever is
        already on that port.
      </div>

      {error && <div style={{ ...errorStyle, marginTop: 10 }}>{error}</div>}

      <div style={dividerStyle} />
      <button onClick={stop} disabled={busy !== null} style={dangerStyle}>
        {busy === "stop" ? "Stopping…" : "Stop synapse"}
      </button>
    </>
  );
}

/* ──────────────────────────────  bits  ────────────────────────────── */

/** The port out of a cosmo:// URL, or "" when there isn't one to read. */
function portOf(url: string): string {
  const m = /:(\d+)\s*$/.exec(url.trim());
  return m ? m[1] : "";
}

function Dot({ live }: { live: boolean }) {
  return (
    <span
      style={{
        width: 7,
        height: 7,
        borderRadius: "50%",
        flexShrink: 0,
        background: live ? C.ok : C.textFaint,
        boxShadow: live ? `0 0 0 3px ${C.ok}26` : "none",
      }}
    />
  );
}

function Fact({ k, v }: { k: string; v: string }) {
  return (
    <div style={{ display: "flex", gap: 8, fontSize: 13.5, lineHeight: 1.7 }}>
      <span style={{ color: C.textFaint, fontWeight: 600, width: 68, flexShrink: 0 }}>{k}</span>
      <span style={{ color: C.text, wordBreak: "break-all" }}>{v}</span>
    </div>
  );
}

function Title({ children }: { children: ReactNode }) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        fontSize: 14.5,
        fontFamily: MONO,
        color: "var(--text)",
        marginBottom: 10,
      }}
    >
      {children}
    </div>
  );
}

function Row({ children }: { children: ReactNode }) {
  return <div style={{ display: "flex", gap: 8 }}>{children}</div>;
}

function Labelled({
  label,
  children,
  flex,
}: {
  label: string;
  children: ReactNode;
  flex?: number;
}) {
  return (
    <div style={{ marginBottom: 10, flex }}>
      <div
        style={{
          fontSize: 13,
          color: "var(--text-faint)",
          textTransform: "uppercase",
          letterSpacing: "0.06em",
          marginBottom: 6,
        }}
      >
        {label}
      </div>
      {children}
    </div>
  );
}

function pillStyle(live: boolean, open: boolean): CSSProperties {
  const tint = live ? "var(--ok)" : "var(--text-faint)";
  return {
    display: "inline-flex",
    alignItems: "center",
    gap: 7,
    padding: "5px 12px",
    borderRadius: 999,
    fontFamily: MONO,
    fontSize: 14,
    whiteSpace: "nowrap",
    cursor: "pointer",
    color: live ? C.ok : C.textDim,
    background: live
      ? "rgba(var(--fg-rgb), 0.04)"
      : open
        ? "rgba(var(--fg-rgb), 0.05)"
        : "transparent",
    border: "1px solid " + (live ? tint + "55" : C.borderStrong),
    transition: "all 0.15s",
  };
}

const panelStyle: CSSProperties = {
  position: "absolute",
  top: "calc(100% + 8px)",
  // Right-anchored, like the settings menu: the indicator lives at the right
  // end of the header, so a left-anchored panel opens off-screen and widens
  // the page.
  right: 0,
  zIndex: 40,
  width: 340,
  background: "var(--bg-overlay)",
  border: "1px solid var(--border-strong)",
  borderRadius: 12,
  padding: 14,
  boxShadow: "0 18px 46px rgba(var(--shadow-rgb), 0.45)",
  WebkitBackdropFilter: "blur(18px)",
  backdropFilter: "blur(18px)",
};

const inputStyle: CSSProperties = {
  width: "100%",
  minWidth: 0,
  background: "var(--bg-elev)",
  border: "1px solid var(--border)",
  borderRadius: 7,
  color: "var(--text)",
  padding: "7px 9px",
  fontSize: 14.5,
  fontFamily: MONO,
  outline: "none",
};

const noteStyle: CSSProperties = {
  fontSize: 13,
  color: "var(--text-faint)",
  lineHeight: 1.6,
};

const codeStyle: CSSProperties = {
  fontFamily: MONO,
  color: "var(--accent2-text)",
};

const warnStyle: CSSProperties = {
  fontSize: 13,
  lineHeight: 1.65,
  color: "var(--text-dim)",
  background: "rgba(var(--fg-rgb), 0.035)",
  border: "1px solid var(--border)",
  borderLeft: "2px solid var(--warn)",
  borderRadius: 8,
  padding: "9px 11px",
};

const factsStyle: CSSProperties = {
  fontFamily: MONO,
  background: "rgba(var(--fg-rgb), 0.03)",
  border: "1px solid var(--border)",
  borderRadius: 8,
  padding: "8px 10px",
};

const dividerStyle: CSSProperties = {
  height: 1,
  background: "var(--border)",
  margin: "12px 0",
};

const errorStyle: CSSProperties = {
  background: "rgba(var(--accent3-rgb), 0.08)",
  border: "1px solid rgba(var(--accent3-rgb), 0.3)",
  color: "var(--accent3)",
  borderRadius: 7,
  padding: "8px 10px",
  fontSize: 13.5,
  lineHeight: 1.5,
  marginBottom: 10,
};

function primaryStyle(enabled: boolean): CSSProperties {
  return {
    width: "100%",
    marginTop: 2,
    padding: "9px 12px",
    borderRadius: 8,
    border: "none",
    background: enabled ? `linear-gradient(135deg, ${C.accent}, ${C.accent2})` : C.bgElev,
    color: enabled ? C.onPrimary : C.textFaint,
    fontWeight: 600,
    fontSize: 14.5,
    fontFamily: MONO,
    cursor: enabled ? "pointer" : "default",
  };
}

const secondaryStyle: CSSProperties = {
  background: "transparent",
  border: "1px solid var(--border-strong)",
  color: "var(--text-dim)",
  borderRadius: 7,
  padding: "7px 11px",
  fontSize: 14,
  fontFamily: MONO,
  cursor: "pointer",
};

const dangerStyle: CSSProperties = {
  width: "100%",
  background: "transparent",
  border: "1px solid rgba(var(--danger-rgb), 0.35)",
  color: "var(--danger)",
  borderRadius: 8,
  padding: "8px 12px",
  fontSize: 14,
  fontFamily: MONO,
  cursor: "pointer",
};

const ghostStyle: CSSProperties = {
  fontSize: 13,
  fontFamily: MONO,
  color: "var(--text-faint)",
  cursor: "pointer",
};
