import { useCallback, useEffect, useRef, useState } from "react";
import { readStoredLayout, storeLayout } from "../brainLayout";
import type { BrainLayout } from "../brainLayout";
import type { SynapseTab } from "../tabs";
import { C, MONO } from "../theme";
import { isPrismError, type Signal } from "../types";
import { useSignalStream } from "../useSignalStream";
import { Constellation } from "./Constellation";
import { Header, type PrismView } from "./Header";
import { Metrics } from "./Metrics";
import { PrismCanvas, type PrismCanvasHandle } from "./PrismCanvas";
import { Sidebar } from "./Sidebar";
import { SignalList } from "./SignalList";
import { SignalTree } from "./SignalTree";
import { Tooltip } from "./Tooltip";

const SIDEBAR_WIDTH = 380;

interface Props {
  tab: SynapseTab;
  /** Only the front tab renders; the rest stay mounted and keep streaming. */
  active: boolean;
  tabs: SynapseTab[];
  activeId: string | null;
  statuses: Record<string, boolean>;
  onSelectTab: (id: string) => void;
  onAddTab: () => void;
  onCloseTab: (id: string) => void;
  onStatus: (id: string, connected: boolean) => void;
}

/**
 * Everything that belongs to one monitored synapse: the signal stream plus the
 * views over it — Brain View (canvas + signal stream), Constellation
 * (execution graph per task run), Signal Tree (nested task diagram), and
 * Metrics (timing). Each open tab mounts its own session, so a background
 * synapse keeps its socket, its buffer and its per-view state; switching tabs
 * is instant and loses nothing.
 */
export function SynapseSession({
  tab,
  active,
  tabs,
  activeId,
  statuses,
  onSelectTab,
  onAddTab,
  onCloseTab,
  onStatus,
}: Props) {
  const [view, setView] = useState<PrismView>("brain");
  // The arrangement is a reading preference, not a property of this synapse,
  // so it is remembered globally and every new session opens with it.
  const [brainLayout, setBrainLayout] = useState<BrainLayout>(readStoredLayout);
  const [paused, setPaused] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [selected, setSelected] = useState<Signal | null>(null);
  const [hover, setHover] = useState<string | null>(null);
  const [mouse, setMouse] = useState({ x: 0, y: 0 });
  const [prismError, setPrismError] = useState<string | null>(null);

  const canvasRef = useRef<PrismCanvasHandle>(null);

  const onSignal = useCallback((sig: Signal) => {
    if (isPrismError(sig)) {
      const msg = sig.payload?.message;
      setPrismError(typeof msg === "string" ? msg : "Prism could not attach to the synapse.");
      return;
    }
    setPrismError(null);
    canvasRef.current?.emit(sig);
  }, []);

  const { connected, signals, neurons, total, clear } = useSignalStream(tab, {
    paused,
    onSignal,
  });

  // Publish this tab's connection state so the switcher can show a dot per synapse.
  useEffect(() => {
    onStatus(tab.id, connected);
  }, [tab.id, connected, onStatus]);

  useEffect(() => {
    if (!active) return;
    const m = (e: MouseEvent) => setMouse({ x: e.clientX, y: e.clientY });
    window.addEventListener("mousemove", m);
    return () => window.removeEventListener("mousemove", m);
  }, [active]);

  const hoverInfo = hover ? neurons.get(hover) ?? null : null;

  return (
    // display:contents keeps the children positioned against the page, exactly
    // as if this wrapper weren't here; display:none parks the whole session.
    <div style={{ display: active ? "contents" : "none" }}>
      <Header
        connected={connected}
        total={total}
        paused={paused}
        sidebarOpen={sidebarOpen}
        tabs={tabs}
        activeId={activeId}
        statuses={statuses}
        view={view}
        brainLayout={brainLayout}
        onSelectView={setView}
        onSelectBrainLayout={(l) => {
          setBrainLayout(l);
          storeLayout(l);
        }}
        onSelectTab={onSelectTab}
        onAddTab={onAddTab}
        onCloseTab={onCloseTab}
        onTogglePause={() => setPaused((p) => !p)}
        onToggleSidebar={() => setSidebarOpen((s) => !s)}
        onClear={() => {
          clear();
          setSelected(null);
        }}
      />

      {/* Brain View stays mounted so the canvas keeps its animation state when
          other views are shown; display:none removes it from layout only. */}
      <div style={{ display: view === "brain" ? "contents" : "none" }}>
        <PrismCanvas
          ref={canvasRef}
          neurons={neurons}
          namespace={tab.namespace}
          sidebarOffset={sidebarOpen ? SIDEBAR_WIDTH : 0}
          layout={brainLayout}
          onHover={setHover}
        />

        {prismError && (
          <div
            style={{
              position: "absolute",
              top: 76,
              left: "50%",
              transform: "translateX(-50%)",
              zIndex: 8,
              background: "rgba(var(--danger-rgb), 0.12)",
              border: "1px solid rgba(var(--danger-rgb), 0.4)",
              color: C.dangerText,
              borderRadius: 10,
              padding: "9px 16px",
              fontSize: 14.5,
              fontFamily: MONO,
              backdropFilter: "blur(12px)",
              WebkitBackdropFilter: "blur(12px)",
            }}
          >
            {prismError}
          </div>
        )}

        {active && <Tooltip neuron={hoverInfo} x={mouse.x} y={mouse.y} />}
        <Sidebar
          open={sidebarOpen}
          width={SIDEBAR_WIDTH}
          signals={signals}
          selected={selected}
          onSelect={setSelected}
        />
      </div>

      {view === "constellation" && <Constellation signals={signals} />}
      {view === "tree" && <SignalTree signals={signals} />}
      {view === "list" && <SignalList signals={signals} />}
      {view === "metrics" && <Metrics signals={signals} />}
    </div>
  );
}
