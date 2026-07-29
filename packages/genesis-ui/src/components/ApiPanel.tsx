import { useMemo, useState } from "react";
import type { CSSProperties } from "react";
import { receptorHttp } from "../api";
import { C, MONO } from "../theme";
import type { InitError, ProxyResult, ReceptorInfo } from "../types";
import { kindColor } from "./CanvasNode";
import { PanelHeader, ghost } from "./TerminalPanel";

/** The three dispatch modes an ApiReceptor's body may ask for. */
const MODES: { id: string; blurb: string }[] = [
  { id: "send", blurb: "Returns as soon as the TASK is on the bus - you get a trace_id, not an answer." },
  { id: "wait", blurb: "Blocks for the terminal Signal and returns its payload." },
  { id: "stream", blurb: "Server-sent events, one per Signal. Shown here as raw text." },
];

/**
 * The ApiReceptor panel: a request builder and its response.
 *
 * Requests go through Genesis rather than straight from the tab. An
 * ApiReceptor sends no access-control-allow-origin, so a direct fetch would
 * be blocked before it left the browser; proxying through the server that
 * served this page sidesteps CORS and turns "nothing is listening on :8000"
 * into a sentence rather than an opaque TypeError.
 */
export function ApiPanel({
  projectPath,
  receptor,
  running,
}: {
  projectPath: string;
  receptor: ReceptorInfo;
  running: boolean;
}) {
  const dispatchPath = String(receptor.config.path ?? "/dispatch");
  const [endpoint, setEndpoint] = useState(dispatchPath);
  const [method, setMethod] = useState("POST");
  const [mode, setMode] = useState("wait");
  const [body, setBody] = useState('{\n  "input": "hello"\n}');
  const [result, setResult] = useState<ProxyResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const base = `http://${receptor.config.host}:${receptor.config.port}`;
  const bodyError = useMemo(() => {
    if (method === "GET" || !body.trim()) return null;
    try {
      JSON.parse(body);
      return null;
    } catch (e) {
      return (e as Error).message;
    }
  }, [body, method]);

  async function send() {
    if (busy || bodyError) return;
    setBusy(true);
    setError(null);
    try {
      let payload: unknown = undefined;
      if (method !== "GET" && body.trim()) {
        payload = JSON.parse(body);
        // mode rides in the body, which is where ApiReceptor.parse() reads it
        // from - it is not a header or a query parameter.
        if (payload && typeof payload === "object" && !Array.isArray(payload)) {
          payload = { ...(payload as Record<string, unknown>), mode };
        }
      }
      setResult(
        await receptorHttp({
          path: projectPath,
          file: receptor.path,
          method,
          endpoint,
          body: payload,
        }),
      );
    } catch (e) {
      setError((e as InitError).error || "Couldn't send that.");
    } finally {
      setBusy(false);
    }
  }

  const color = kindColor().receptor;

  return (
    <div style={{ flex: 1, display: "flex", flexDirection: "column", minHeight: 0 }}>
      <PanelHeader
        receptor={receptor}
        live={running}
        running={running}
        right={
          <button onClick={send} disabled={busy || !!bodyError} style={sendStyle(!busy && !bodyError)}>
            {busy ? "sending…" : "Send"}
          </button>
        }
      />

      <div style={{ flex: 1, display: "flex", minHeight: 0 }}>
        {/* request */}
        <div
          style={{
            width: "50%",
            display: "flex",
            flexDirection: "column",
            borderRight: `1px solid ${C.border}`,
            minWidth: 0,
          }}
        >
          <div style={{ padding: "12px 16px", borderBottom: `1px solid ${C.border}` }}>
            <div style={{ display: "flex", gap: 7, alignItems: "center" }}>
              <select value={method} onChange={(e) => setMethod(e.target.value)} style={{ ...field, width: 86 }}>
                {["POST", "GET", "PUT", "DELETE"].map((m) => (
                  <option key={m}>{m}</option>
                ))}
              </select>
              <span style={{ fontFamily: MONO, fontSize: 13, color: C.textFaint, fontWeight: 600, }}>{base}</span>
              <input
                value={endpoint}
                onChange={(e) => setEndpoint(e.target.value)}
                style={{ ...field, flex: 1 }}
              />
            </div>

            <div style={{ display: "flex", gap: 6, marginTop: 10, alignItems: "center" }}>
              <span style={label}>mode</span>
              {MODES.map((m) => {
                const on = mode === m.id;
                return (
                  <button
                    key={m.id}
                    title={m.blurb}
                    onClick={() => setMode(m.id)}
                    style={{
                      ...ghost,
                      color: on ? color : C.textDim,
                      borderColor: on ? color + "55" : C.border,
                      background: on ? color + "12" : "transparent",
                    }}
                  >
                    {m.id}
                  </button>
                );
              })}
            </div>
            <div style={{ fontSize: 13, color: C.textFaint, fontWeight: 600, marginTop: 7, lineHeight: 1.5 }}>
              {MODES.find((m) => m.id === mode)?.blurb} Merged into the JSON body as{" "}
              <code style={{ fontFamily: MONO }}>"mode"</code>, which is where{" "}
              <code style={{ fontFamily: MONO }}>parse()</code> reads it from.
            </div>
          </div>

          <div style={{ padding: "10px 16px 4px", display: "flex", alignItems: "center", gap: 8 }}>
            <span style={label}>body</span>
            {bodyError && (
              <span style={{ fontSize: 13, color: C.accent3 }}>invalid JSON · {bodyError}</span>
            )}
          </div>
          <textarea
            value={body}
            onChange={(e) => setBody(e.target.value)}
            spellCheck={false}
            style={{
              flex: 1,
              margin: "0 16px 16px",
              background: "var(--bg-well)",
              border: `1px solid ${bodyError ? C.accent3 + "66" : C.border}`,
              borderRadius: 9,
              color: C.text,
              padding: 12,
              fontFamily: MONO,
              fontSize: 14.5,
              lineHeight: 1.6,
              resize: "none",
              outline: "none",
            }}
          />
        </div>

        {/* response */}
        <div style={{ width: "50%", display: "flex", flexDirection: "column", minWidth: 0 }}>
          <div
            style={{
              padding: "12px 16px",
              borderBottom: `1px solid ${C.border}`,
              display: "flex",
              alignItems: "center",
              gap: 10,
              minHeight: 43,
            }}
          >
            <span style={label}>response</span>
            {result?.status != null && (
              <span
                style={{
                  fontFamily: MONO,
                  fontSize: 13.5,
                  color: result.status < 300 ? C.ok : result.status < 500 ? C.warn : C.danger,
                }}
              >
                {result.status}
              </span>
            )}
            {result && (
              <span style={{ fontFamily: MONO, fontSize: 13, color: C.textFaint, fontWeight: 600, }}>
                {result.elapsed_ms} ms
              </span>
            )}
          </div>

          <div
            style={{
              flex: 1,
              overflow: "auto",
              padding: 16,
              fontFamily: MONO,
              fontSize: 14.5,
              lineHeight: 1.6,
              whiteSpace: "pre-wrap",
              wordBreak: "break-word",
              color: C.text,
            }}
          >
            {error && <span style={{ color: C.accent3 }}>{error}</span>}
            {!error && result && !result.ok && (
              <span style={{ color: C.accent3 }}>{result.error}</span>
            )}
            {!error && result?.ok && (
              <>{result.json !== null && result.json !== undefined
                ? JSON.stringify(result.json, null, 2)
                : result.text}</>
            )}
            {!error && !result && (
              <span style={{ color: C.textFaint, fontWeight: 600, }}>
                {running
                  ? "Send a request and the reply lands here."
                  : "brain.py isn't running, so nothing is listening on this port yet."}
              </span>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

const field: CSSProperties = {
  background: "var(--bg-elev)",
  border: "1px solid var(--border)",
  borderRadius: 7,
  color: "var(--text)",
  padding: "6px 9px",
  fontFamily: MONO,
  fontSize: 14,
  outline: "none",
};

const label: CSSProperties = {
  fontSize: 13,
  color: "var(--text-faint)",
  textTransform: "uppercase",
  letterSpacing: "0.08em",
};

function sendStyle(enabled: boolean): CSSProperties {
  return {
    padding: "5px 16px",
    borderRadius: 7,
    border: "none",
    background: enabled ? `linear-gradient(135deg, ${C.accent}, ${C.accent2})` : C.bgElev,
    color: enabled ? C.onPrimary : C.textFaint,
    fontWeight: 600,
    fontSize: 13.5,
    fontFamily: MONO,
    cursor: enabled ? "pointer" : "default",
  };
}
