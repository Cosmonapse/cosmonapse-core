import { C, MONO } from "../theme";
import type { SynapseTab } from "../tabs";
import { Logo } from "./Logo";
import { SynapseSwitcher } from "./SynapseSwitcher";

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
  /** Every synapse Prism currently holds open, and which one is in front. */
  tabs: SynapseTab[];
  activeId: string | null;
  statuses: Record<string, boolean>;
  view: PrismView;
  onSelectView: (v: PrismView) => void;
  onSelectTab: (id: string) => void;
  onAddTab: () => void;
  onCloseTab: (id: string) => void;
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
  tabs,
  activeId,
  statuses,
  view,
  onSelectView,
  onSelectTab,
  onAddTab,
  onCloseTab,
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

      {/* Which synapse is in front - and every other one, one click away. */}
      <SynapseSwitcher
        tabs={tabs}
        activeId={activeId}
        statuses={statuses}
        onSelect={onSelectTab}
        onAdd={onAddTab}
        onClose={onCloseTab}
      />

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
