import { C, MONO, colorFor } from "../theme";
import type { Signal } from "../types";

interface Props {
  open: boolean;
  width: number;
  signals: Signal[];
  selected: Signal | null;
  onSelect: (sig: Signal | null) => void;
}

export function Sidebar({ open, width, signals, selected, onSelect }: Props) {
  return (
    <aside
      style={{
        position: "absolute",
        top: 64,
        right: 0,
        bottom: 0,
        width: open ? width : 0,
        background: "rgba(7,8,12,0.85)",
        WebkitBackdropFilter: "blur(20px)",
        backdropFilter: "blur(20px)",
        borderLeft: open ? "1px solid " + C.border : "none",
        transition: "width 0.25s ease",
        overflow: "hidden",
        zIndex: 4,
        display: "flex",
        flexDirection: "column",
      }}
    >
      <div
        style={{
          flexShrink: 0,
          padding: "14px 16px",
          borderBottom: "1px solid " + C.border,
          display: "flex",
          alignItems: "center",
          gap: 10,
        }}
      >
        <span
          style={{
            fontFamily: MONO,
            fontSize: 11,
            color: C.accent,
            letterSpacing: "0.14em",
            textTransform: "uppercase",
          }}
        >
          Signal stream
        </span>
        <span style={{ marginLeft: "auto", color: C.textFaint, fontSize: 12, fontFamily: MONO }}>
          {signals.length}
        </span>
      </div>
      <div style={{ flex: 1, overflowY: "auto" }}>
        {signals.length === 0 && (
          <div style={{ padding: 48, textAlign: "center", color: C.textFaint, fontSize: 13 }}>
            Waiting for signals…
          </div>
        )}
        {signals.map((sig, i) => {
          const c = colorFor(sig.type);
          const ts = safeTime(sig.ts);
          const isSel = selected === sig;
          return (
            <div
              key={sig.id || i}
              onClick={() => onSelect(isSel ? null : sig)}
              style={{
                padding: "10px 16px",
                cursor: "pointer",
                borderBottom: "1px solid " + C.border,
                background: isSel ? "rgba(139,92,246,0.08)" : "transparent",
                transition: "background 0.15s",
              }}
            >
              <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
                <span
                  style={{
                    display: "inline-block",
                    width: 8,
                    height: 8,
                    borderRadius: "50%",
                    background: c,
                    boxShadow: `0 0 6px ${c}`,
                  }}
                />
                <span
                  style={{
                    color: c,
                    fontFamily: MONO,
                    fontSize: 11.5,
                    fontWeight: 600,
                    letterSpacing: "0.03em",
                  }}
                >
                  {sig.type}
                </span>
                <span
                  style={{
                    marginLeft: "auto",
                    color: C.textFaint,
                    fontSize: 10.5,
                    fontFamily: MONO,
                  }}
                >
                  {ts}
                </span>
              </div>
              <div
                style={{
                  color: C.textDim,
                  fontSize: 11.5,
                  fontFamily: MONO,
                  whiteSpace: "nowrap",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                }}
              >
                {sig.neuron || " - "}
                <span style={{ color: C.textFaint }}> · {(sig.trace_id || "").slice(4, 12)}</span>
              </div>
              {isSel && sig.payload && (
                <pre
                  style={{
                    marginTop: 8,
                    padding: 8,
                    background: "rgba(0,0,0,0.3)",
                    borderRadius: 6,
                    color: C.textDim,
                    fontSize: 10.5,
                    fontFamily: MONO,
                    whiteSpace: "pre-wrap",
                    wordBreak: "break-all",
                    maxHeight: 240,
                    overflowY: "auto",
                  }}
                >
                  {JSON.stringify(sig.payload, null, 2)}
                </pre>
              )}
            </div>
          );
        })}
      </div>
    </aside>
  );
}

function safeTime(ts: string): string {
  const d = new Date(ts);
  return isNaN(d.getTime()) ? ts : d.toISOString().slice(11, 23);
}
