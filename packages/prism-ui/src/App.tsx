import { useCallback, useEffect, useRef, useState } from "react";
import { ConnectForm } from "./components/ConnectForm";
import { Header } from "./components/Header";
import { PrismCanvas, type PrismCanvasHandle } from "./components/PrismCanvas";
import { Sidebar } from "./components/Sidebar";
import { Tooltip } from "./components/Tooltip";
import { MONO } from "./theme";
import { isPrismError, type Signal } from "./types";
import { useSignalStream, type SynapseTarget } from "./useSignalStream";

const SIDEBAR_WIDTH = 380;

function targetFromLocation(): SynapseTarget | null {
  const p = new URLSearchParams(location.search);
  const url = p.get("url");
  if (!url) return null;
  return { url, namespace: p.get("namespace") || "dev" };
}

export function App() {
  const [target, setTarget] = useState<SynapseTarget | null>(targetFromLocation);
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

  // Keep the URL in sync so the view is shareable / reloadable.
  useEffect(() => {
    if (!target) return;
    const qs = new URLSearchParams({ url: target.url, namespace: target.namespace }).toString();
    const next = `${location.pathname}?${qs}`;
    if (location.search !== `?${qs}`) history.replaceState(null, "", next);
  }, [target]);

  const onConnect = useCallback((t: SynapseTarget) => {
    setPrismError(null);
    setTarget(t);
  }, []);

  const onBack = useCallback(() => {
    history.replaceState(null, "", location.pathname);
    setTarget(null);
    setPrismError(null);
  }, []);

  if (!target) {
    return <ConnectForm initial={targetFromLocation() ?? undefined} onConnect={onConnect} />;
  }

  const hoverInfo = hover ? neurons.get(hover) ?? null : null;

  return (
    <>
      <Header
        target={target}
        connected={connected}
        total={total}
        paused={paused}
        sidebarOpen={sidebarOpen}
        onTogglePause={() => setPaused((p) => !p)}
        onToggleSidebar={() => setSidebarOpen((s) => !s)}
        onClear={() => {
          clear();
          setSelected(null);
        }}
        onBack={onBack}
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

      <Tooltip neuron={hoverInfo} x={mouse.x} y={mouse.y} />
      <Sidebar
        open={sidebarOpen}
        width={SIDEBAR_WIDTH}
        signals={signals}
        selected={selected}
        onSelect={setSelected}
      />
    </>
  );
}
