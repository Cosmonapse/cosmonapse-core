import { useCallback, useEffect, useRef, useState } from "react";
import { MONO } from "../theme";
import { isPrismError, type Signal } from "../types";
import { useSignalStream, type SynapseTarget } from "../useSignalStream";
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
  target: SynapseTarget;
  onDisconnect: () => void;
}

/**
 * Everything that belongs to the monitored synapse: the signal stream plus the
 * views over it — Brain View (canvas + signal stream), Constellation
 * (execution graph per task run), Signal Tree (nested task diagram), and
 * Metrics (timing). The stream keeps flowing while
 * inactive views stay mounted, so switching tabs is instant.
 */
export function SynapseSession({ target, onDisconnect }: Props) {
  const [view, setView] = useState<PrismView>("brain");
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
    const m = (e: MouseEvent) => setMouse({ x: e.clientX, y: e.clientY });
    window.addEventListener("mousemove", m);
    return () => window.removeEventListener("mousemove", m);
  }, []);

  const hoverInfo = hover ? neurons.get(hover) ?? null : null;

  return (
    <>
      <Header
        connected={connected}
        total={total}
        paused={paused}
        sidebarOpen={sidebarOpen}
        namespace={target.namespace}
        url={target.url}
        view={view}
        onSelectView={setView}
        onDisconnect={onDisconnect}
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

        <Tooltip neuron={hoverInfo} x={mouse.x} y={mouse.y} />
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
    </>
  );
}
