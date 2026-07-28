import { useEffect, useMemo, useState } from "react";
import { readScaffold } from "../api";
import type { ScaffoldResult } from "../types";
import { C, MONO } from "../theme";
import { CanvasNode } from "./CanvasNode";
import type { CanvasNodeData, NodeKind } from "./CanvasNode";
import { Logo } from "./Logo";

const CANVAS_W = 1600;
const CANVAS_H = 1000;
const RADIUS = 320;

function layoutKey(path: string) {
  return `genesis:layout:${path}`;
}

/** Evenly place nodes on a circle around the synapse at canvas center. */
function initialLayout(scaffold: ScaffoldResult): CanvasNodeData[] {
  const cx = CANVAS_W / 2;
  const cy = CANVAS_H / 2;
  const nodes: CanvasNodeData[] = [
    { key: "synapse", kind: "synapse", id: scaffold.synapse.id, x: cx, y: cy },
  ];

  const orbit: { kind: NodeKind; id: string; sublabel: string }[] = [
    ...scaffold.neurons.map((n) => ({ kind: "neuron" as NodeKind, id: n.id, sublabel: n.file })),
    ...scaffold.effectors.map((e) => ({ kind: "effector" as NodeKind, id: e.id, sublabel: e.file })),
    ...scaffold.engrams.map((e) => ({ kind: "engram" as NodeKind, id: e.id, sublabel: e.file })),
  ];

  const n = orbit.length || 1;
  orbit.forEach((item, i) => {
    const angle = (2 * Math.PI * i) / n - Math.PI / 2;
    nodes.push({
      key: `${item.kind}:${item.id}`,
      kind: item.kind,
      id: item.id,
      sublabel: item.sublabel,
      x: cx + RADIUS * Math.cos(angle),
      y: cy + RADIUS * Math.sin(angle),
    });
  });

  return nodes;
}

function loadLayout(scaffold: ScaffoldResult): CanvasNodeData[] {
  const fresh = initialLayout(scaffold);
  try {
    const raw = localStorage.getItem(layoutKey(scaffold.path));
    if (!raw) return fresh;
    const saved: Record<string, { x: number; y: number }> = JSON.parse(raw);
    return fresh.map((n) => (saved[n.key] ? { ...n, ...saved[n.key] } : n));
  } catch {
    return fresh;
  }
}

function saveLayout(path: string, nodes: CanvasNodeData[]) {
  const positions: Record<string, { x: number; y: number }> = {};
  for (const n of nodes) positions[n.key] = { x: n.x, y: n.y };
  try {
    localStorage.setItem(layoutKey(path), JSON.stringify(positions));
  } catch {
    // best-effort - a full localStorage shouldn't break the canvas
  }
}

export function GenesisCanvas({
  initialPath,
  onBack,
}: {
  initialPath: string;
  onBack: () => void;
}) {
  const [scaffold, setScaffold] = useState<ScaffoldResult | null>(null);
  const [nodes, setNodes] = useState<CanvasNodeData[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  function load() {
    readScaffold(initialPath)
      .then((s) => {
        setScaffold(s);
        setNodes(loadLayout(s));
        setError(null);
      })
      .catch(() => setError("Couldn't read the scaffolded project."));
  }

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(load, [initialPath]);

  const edges = useMemo(() => {
    const synapse = nodes.find((n) => n.kind === "synapse");
    if (!synapse) return [];
    return nodes.filter((n) => n.kind !== "synapse").map((n) => ({ from: synapse, to: n }));
  }, [nodes]);

  function onDrag(key: string, x: number, y: number) {
    setNodes((prev) => {
      const next = prev.map((n) => (n.key === key ? { ...n, x, y } : n));
      if (scaffold) saveLayout(scaffold.path, next);
      return next;
    });
  }

  const selectedNode = nodes.find((n) => n.key === selected) ?? null;

  return (
    <div style={{ height: "100vh", display: "flex", flexDirection: "column" }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "14px 20px",
          borderBottom: `1px solid ${C.border}`,
          background: C.bgCard,
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 18 }}>
          <Logo />
          {scaffold && (
            <div style={{ fontFamily: MONO, fontSize: 12, color: C.textDim }}>
              {scaffold.project} <span style={{ color: C.textFaint }}>· {scaffold.path}</span>
            </div>
          )}
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <button onClick={load} style={ghostBtnStyle}>
            Reload
          </button>
          <button onClick={onBack} style={ghostBtnStyle}>
            &larr; New brain
          </button>
        </div>
      </div>

      <div style={{ flex: 1, overflow: "auto", position: "relative" }}>
        {error && (
          <div style={{ padding: 24, color: C.accent3, fontFamily: MONO, fontSize: 13 }}>{error}</div>
        )}
        {!error && (
          <div style={{ position: "relative", width: CANVAS_W, height: CANVAS_H }}>
            <svg
              width={CANVAS_W}
              height={CANVAS_H}
              style={{ position: "absolute", inset: 0, pointerEvents: "none" }}
            >
              {edges.map((e) => (
                <line
                  key={e.to.key}
                  x1={e.from.x}
                  y1={e.from.y}
                  x2={e.to.x}
                  y2={e.to.y}
                  stroke={C.borderStrong}
                  strokeWidth={1.5}
                />
              ))}
            </svg>
            {nodes.map((n) => (
              <CanvasNode key={n.key} node={n} onDrag={onDrag} onSelect={setSelected} selected={selected === n.key} />
            ))}
          </div>
        )}
      </div>

      {selectedNode && (
        <div
          style={{
            position: "fixed",
            right: 20,
            bottom: 20,
            background: C.bgCard,
            border: `1px solid ${C.borderStrong}`,
            borderRadius: 10,
            padding: "12px 16px",
            fontFamily: MONO,
            fontSize: 12,
            color: C.textDim,
            maxWidth: 260,
          }}
        >
          <div style={{ color: C.text, marginBottom: 4 }}>{selectedNode.id}</div>
          {selectedNode.sublabel && <div style={{ color: C.textFaint }}>{selectedNode.sublabel}</div>}
        </div>
      )}
    </div>
  );
}

const ghostBtnStyle = {
  background: "transparent",
  border: `1px solid ${C.border}`,
  borderRadius: 8,
  color: C.textDim,
  padding: "6px 12px",
  fontSize: 12,
  cursor: "pointer",
};
