import { C, MONO } from "../theme";
import { Logo } from "./Logo";

export type PrismView = "brain" | "constellation" | "tree" | "list" | "metrics";

export const VIEWS: { id: PrismView; label: string }[] = [
  { id: "brain", label: "Brain View" },
  { id: "constellation", label: "Constellation" },
  { id: "tree", label: "Signal Tree" },
  { id: "list", label: "Signal List" },
  { id: "metrics", label: "Metrics" },
];

interface Props {
  connected: boolean;
  total: number;
  paused: boolean;
  sidebarOpen: boolean;
  /** The single monitored synapse. */
  namespace: string;
  url: string;
  view: PrismView;
  onSelectView: (v: PrismView) => void;
  onDisconnect: () => void;
  onTogglePause: () => void;
  onToggleSidebar: () => void;
  onClear: () => void;
}

function btn(active?: string | null): React.CSSProperties {
  return {
    background: active ? active + "18" : "transparent",
    border: "1px solid " + (active ? active + "40" : C.borderStrong),
    color: active || C.textDim,
    borderRadius: 8,
    padding: "5px 12px",
    fontSize: 12,
    fontFamily: MONO,
    cursor: "pointer",
    transition: "all 0.15s",
  };
}

export function Header({
  connected,
  total,
  paused,
  sidebarOpen,
  namespace,
  url,
  view,
  onSelectView,
  onDisconnect,
  onTogglePause,
  onToggleSidebar,
  onClear,
}: Props) {
  return (
    <div
      style={{
        position: "absolute",
        top: 0,
        left: 0,
        right: 0,
        zIndex: 5,
        display: "flex",
        alignItems: "center",
        gap: 14,
        padding: "12px 20px",
        background: "rgba(7,8,12,0.7)",
        WebkitBackdropFilter: "blur(20px)",
        backdropFilter: "blur(20px)",
        borderBottom: "1px solid " + C.border,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexShrink: 0 }}>
        <Logo size={30} />
        <span className="brand-word" style={{ fontWeight: 700, fontSize: 17 }}>Cosmonapse</span>
        <span style={{ color: C.textDim, fontWeight: 500, fontSize: 17 }}>Prism</span>
      </div>
      <span style={{ color: C.textFaint, flexShrink: 0 }}>│</span>

      {/* The single monitored synapse */}
      <div
        title={`${shortUrl(url)} /${namespace}`}
        style={{
          display: "flex",
          alignItems: "center",
          gap: 7,
          flexShrink: 0,
          padding: "4px 10px",
          borderRadius: 8,
          fontFamily: MONO,
          fontSize: 12,
          background: "rgba(139,92,246,0.13)",
          border: "1px solid rgba(139,92,246,0.45)",
        }}
      >
        <span
          style={{
            width: 6,
            height: 6,
            borderRadius: "50%",
            flexShrink: 0,
            background: connected ? "#34d399" : "#f87171",
            boxShadow: `0 0 5px ${connected ? "#34d399" : "#f87171"}`,
          }}
        />
        <span style={{ color: C.accent2, whiteSpace: "nowrap" }}>{shortUrl(url)}</span>
        <span style={{ color: C.textDim, whiteSpace: "nowrap" }}>/{namespace}</span>
        <span
          onClick={onDisconnect}
          title="Disconnect / monitor another synapse"
          style={{ color: C.textFaint, paddingLeft: 2, fontSize: 13, lineHeight: 1, cursor: "pointer" }}
        >
          ×
        </span>
      </div>

      <span style={{ color: C.textFaint, flexShrink: 0 }}>│</span>

      {/* View switcher for this namespace */}
      <div style={{ display: "flex", alignItems: "center", gap: 6, flexShrink: 0 }}>
        {VIEWS.map((v) => {
          const on = v.id === view;
          return (
            <div
              key={v.id}
              onClick={() => onSelectView(v.id)}
              style={{
                flexShrink: 0,
                padding: "4px 12px",
                borderRadius: 8,
                cursor: "pointer",
                fontFamily: MONO,
                fontSize: 12,
                whiteSpace: "nowrap",
                color: on ? C.accent2 : C.textDim,
                background: on ? "rgba(34,211,238,0.12)" : "transparent",
                border: "1px solid " + (on ? "rgba(34,211,238,0.4)" : C.border),
                transition: "all 0.15s",
              }}
            >
              {v.label}
            </div>
          );
        })}
      </div>

      <div
        style={{
          marginLeft: "auto",
          display: "flex",
          alignItems: "center",
          gap: 10,
          flexShrink: 0,
        }}
      >
        <span
          style={{
            color: connected ? "#34d399" : "#f87171",
            fontSize: 12,
            fontFamily: MONO,
          }}
        >
          {connected ? "● connected" : "○ reconnecting…"}
        </span>
        <span style={{ color: C.textFaint, fontSize: 12, fontFamily: MONO }}>
          {total} signals
        </span>
        <button onClick={onTogglePause} style={btn(paused ? "#fbbf24" : null)}>
          {paused ? "▶ resume" : "⏸ pause"}
        </button>
        <button onClick={onClear} style={btn(null)}>
          clear
        </button>
        {view === "brain" && (
          <button onClick={onToggleSidebar} style={btn(sidebarOpen ? "#a78bfa" : null)}>
            {sidebarOpen ? "hide signals ›" : "‹ signals"}
          </button>
        )}
      </div>
    </div>
  );
}

function shortUrl(url: string): string {
  return url.replace(/^[a-z]+:\/\//i, "");
}
