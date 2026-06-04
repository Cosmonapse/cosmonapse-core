import { C, MONO } from "../theme";
import { Logo } from "./Logo";
import type { SynapseTarget } from "../useSignalStream";

interface Props {
  target: SynapseTarget;
  connected: boolean;
  total: number;
  paused: boolean;
  sidebarOpen: boolean;
  onTogglePause: () => void;
  onToggleSidebar: () => void;
  onClear: () => void;
  onBack: () => void;
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
  target,
  connected,
  total,
  paused,
  sidebarOpen,
  onTogglePause,
  onToggleSidebar,
  onClear,
  onBack,
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
      <div
        onClick={onBack}
        style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer" }}
      >
        <Logo size={22} />
        <span style={{ fontWeight: 700, fontSize: 15 }}>Cosmonapse</span>
        <span style={{ color: C.textDim, fontWeight: 500, fontSize: 15 }}>Prism</span>
      </div>
      <span style={{ color: C.textFaint }}>│</span>
      <span style={{ color: C.accent2, fontFamily: MONO, fontSize: 12.5 }}>{target.url}</span>
      <span style={{ color: C.textFaint, fontFamily: MONO, fontSize: 12.5 }}>
        /{target.namespace}
      </span>
      <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 10 }}>
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
        <button onClick={onToggleSidebar} style={btn(sidebarOpen ? "#a78bfa" : null)}>
          {sidebarOpen ? "hide signals ›" : "‹ signals"}
        </button>
      </div>
    </div>
  );
}
