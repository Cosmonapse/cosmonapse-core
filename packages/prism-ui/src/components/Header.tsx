import { C, MONO } from "../theme";
import type { BrainLayout } from "../brainLayout";
import type { SynapseTab } from "../tabs";
import { Logo } from "./Logo";
import { ThemeToggle } from "./ThemeToggle";
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
  /** Brain View only: radial soma, or the horizontal bus. */
  brainLayout: BrainLayout;
  onSelectView: (v: PrismView) => void;
  onSelectBrainLayout: (l: BrainLayout) => void;
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
    fontSize: 14.5,
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
  brainLayout,
  onSelectView,
  onSelectBrainLayout,
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
        background: "var(--bg-header)",
        WebkitBackdropFilter: "blur(20px)",
        backdropFilter: "blur(20px)",
        borderBottom: "1px solid " + C.border,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexShrink: 0 }}>
        <Logo size={30} />
        <span className="brand-word" style={{ fontWeight: 700, fontSize: 18 }}>Cosmonapse</span>
        <span style={{ color: C.textDim, fontWeight: 500, fontSize: 18 }}>Prism</span>
      </div>
      <span style={{ color: C.textFaint, fontWeight: 600, flexShrink: 0 }}>│</span>

      {/* Which synapse is in front - and every other one, one click away. */}
      <SynapseSwitcher
        tabs={tabs}
        activeId={activeId}
        statuses={statuses}
        onSelect={onSelectTab}
        onAdd={onAddTab}
        onClose={onCloseTab}
      />

      <span style={{ color: C.textFaint, fontWeight: 600, flexShrink: 0 }}>│</span>

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
                fontSize: 14.5,
                whiteSpace: "nowrap",
                color: on ? C.accent2 : C.textDim,
                background: on ? "rgba(var(--accent2-rgb), 0.12)" : "transparent",
                border: "1px solid " + (on ? "rgba(var(--accent2-rgb), 0.4)" : C.border),
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
            color: connected ? C.okSoft : C.danger,
            fontSize: 14.5,
            fontFamily: MONO,
          }}
        >
          {connected ? "● connected" : "○ reconnecting…"}
        </span>
        <span style={{ color: C.textFaint, fontWeight: 600, fontSize: 14.5, fontFamily: MONO }}>
          {total} signals
        </span>
        <button onClick={onTogglePause} style={btn(paused ? C.warn : null)}>
          {paused ? "▶ resume" : "⏸ pause"}
        </button>
        <button onClick={onClear} style={btn(null)}>
          clear
        </button>
        {view === "brain" && (
          <>
            <LayoutToggle value={brainLayout} onSelect={onSelectBrainLayout} />
            <button onClick={onToggleSidebar} style={btn(sidebarOpen ? C.engram : null)}>
              {sidebarOpen ? "hide signals ›" : "‹ signals"}
            </button>
          </>
        )}
        <ThemeToggle />
      </div>
    </div>
  );
}

/**
 * Radial or bus - the same brain, arranged two ways. A two-segment pill
 * rather than a single toggle button, so the arrangement you are *not*
 * looking at is still named on screen.
 */
const LAYOUTS: { id: BrainLayout; glyph: string; title: string }[] = [
  { id: "radial", glyph: "◎", title: "Radial - synapse at the centre, participants ringing it" },
  { id: "bus", glyph: "≡", title: "Bus - synapse as a horizontal bar, receptors above it" },
];

function LayoutToggle({ value, onSelect }: {
  value: BrainLayout;
  onSelect: (l: BrainLayout) => void;
}) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 2,
        padding: 2,
        borderRadius: 9,
        border: "1px solid " + C.borderStrong,
      }}
    >
      {LAYOUTS.map((l) => {
        const on = l.id === value;
        return (
          <div
            key={l.id}
            title={l.title}
            onClick={() => onSelect(l.id)}
            style={{
              padding: "3px 10px",
              borderRadius: 7,
              cursor: "pointer",
              fontFamily: MONO,
              fontSize: 14.5,
              lineHeight: 1.2,
              whiteSpace: "nowrap",
              color: on ? C.accent2 : C.textDim,
              background: on ? "rgba(var(--accent2-rgb), 0.14)" : "transparent",
              transition: "all 0.15s",
            }}
          >
            {l.glyph} {l.id}
          </div>
        );
      })}
    </div>
  );
}
