import { C, MONO } from "../theme";
import { Logo } from "./Logo";

export interface TabInfo {
  id: string;
  /** namespace */
  label: string;
  /** url without scheme */
  sublabel: string;
  active: boolean;
  connected: boolean;
}

interface Props {
  connected: boolean;
  total: number;
  paused: boolean;
  sidebarOpen: boolean;
  tabs: TabInfo[];
  onSelectTab: (id: string) => void;
  onCloseTab: (id: string) => void;
  onNewTab: () => void;
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
  onSelectTab,
  onCloseTab,
  onNewTab,
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

      {/* Synapse tabs — one pill per monitored synapse */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          minWidth: 0,
          overflowX: "auto",
          scrollbarWidth: "none",
        }}
      >
        {tabs.map((t) => (
          <div
            key={t.id}
            onClick={() => onSelectTab(t.id)}
            title={`${t.sublabel} /${t.label}`}
            style={{
              display: "flex",
              alignItems: "center",
              gap: 7,
              flexShrink: 0,
              padding: "4px 10px",
              borderRadius: 8,
              cursor: "pointer",
              fontFamily: MONO,
              fontSize: 12,
              background: t.active ? "rgba(139,92,246,0.13)" : "transparent",
              border: "1px solid " + (t.active ? "rgba(139,92,246,0.45)" : C.border),
              transition: "all 0.15s",
            }}
          >
            <span
              style={{
                width: 6,
                height: 6,
                borderRadius: "50%",
                flexShrink: 0,
                background: t.connected ? "#34d399" : "#f87171",
                boxShadow: `0 0 5px ${t.connected ? "#34d399" : "#f87171"}`,
              }}
            />
            <span style={{ color: t.active ? C.accent2 : C.textDim, whiteSpace: "nowrap" }}>
              {t.sublabel}
            </span>
            <span style={{ color: t.active ? C.textDim : C.textFaint, whiteSpace: "nowrap" }}>
              /{t.label}
            </span>
            <span
              onClick={(e) => {
                e.stopPropagation();
                onCloseTab(t.id);
              }}
              title="Close tab"
              style={{
                color: C.textFaint,
                paddingLeft: 2,
                fontSize: 13,
                lineHeight: 1,
                cursor: "pointer",
              }}
            >
              ×
            </span>
          </div>
        ))}
        <button
          onClick={onNewTab}
          title="Monitor another synapse"
          style={{
            ...btn(null),
            padding: "3px 9px",
            fontSize: 14,
            lineHeight: 1.2,
            flexShrink: 0,
          }}
        >
          +
        </button>
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
        <button onClick={onToggleSidebar} style={btn(sidebarOpen ? "#a78bfa" : null)}>
          {sidebarOpen ? "hide signals ›" : "‹ signals"}
        </button>
      </div>
    </div>
  );
}
