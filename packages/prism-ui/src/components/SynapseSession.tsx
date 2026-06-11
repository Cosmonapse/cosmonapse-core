import { useCallback, useEffect, useRef, useState } from "react";
import { MONO } from "../theme";
import { isPrismError, type Signal } from "../types";
import { useSignalStream, type SynapseTarget } from "../useSignalStream";
import { Header, type TabInfo } from "./Header";
import { PrismCanvas, type PrismCanvasHandle } from "./PrismCanvas";
import { Sidebar } from "./Sidebar";
import { Tooltip } from "./Tooltip";

const SIDEBAR_WIDTH = 380;

interface Props {
  target: SynapseTarget;
  /** Whether this session's tab is the visible one. Inactive sessions stay
   *  mounted so their WebSocket keeps streaming in the background. */
  active: boolean;
  tabs: TabInfo[];
  onConnectedChange: (connected: boolean) => void;
  onSelectTab: (id: string) => void;
  onCloseTab: (id: string) => void;
  onNewTab: () => void;
}

/**
 * Everything that belongs to ONE synapse connection: signal stream, canvas,
 * sidebar, pause/clear state. The App mounts one of these per tab and keeps
 * them all alive; only the active one renders its chrome.
 */
export function SynapseSession({
  target,
  active,
  tabs,
  onConnectedChange,
  onSelectTab,
  onCloseTab,
  onNewTab,
}: Props) {
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

  const { connected, signals, neurons, total, clear } = useSignalStream(target, {
    paused,
    onSignal,
  });

  useEffect(() => {
    onConnectedChange(connected);
  }, [connected, onConnectedChange]);

  useEffect(() => {
    if (!active) return;
    const m = (e: MouseEvent) => setMouse({ x: e.clientX, y: e.clientY });
    window.addEventListener("mousemove", m);
    return () => window.removeEventListener("mousemove", m);
  }, [active]);

  const hoverInfo = hover ? neurons.get(hover) ?? null : null;

  return (
    <div style={{ display: active ? "contents" : "none" }}>
      <Header
        connected={connected}
        total={total}
        paused={paused}
        sidebarOpen={sidebarOpen}
        tabs={tabs}
        onSelectTab={onSelectTab}
        onCloseTab={onCloseTab}
        onNewTab={onNewTab}
        onTogglePause={() => setPaused((p) => !p)}
        onToggleSidebar={() => setSidebarOpen((s) => !s)}
        onClear={() => {
          clear();
          setSelected(null);
        }}
      />

      <PrismCanvas
        ref={canvasRef}
        neurons={neurons}
        namespace={target.namespace}
        sidebarOffset={sidebarOpen ? SIDEBAR_WIDTH : 0}
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
            background: "rgba(248,113,113,0.12)",
            border: "1px solid rgba(248,113,113,0.4)",
            color: "#fecaca",
            borderRadius: 10,
            padding: "9px 16px",
            fontSize: 12.5,
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
  );
}
