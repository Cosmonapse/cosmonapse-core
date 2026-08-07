import { useMemo, useState } from "react";
import type { CSSProperties } from "react";
import type { ComponentResult, RemoveResult, ScaffoldResult } from "../types";
import { C, MONO } from "../theme";
import { CanvasDefs, CanvasNode, cup, fileOf, kindColor } from "./CanvasNode";
import type { CanvasNodeData, NodeKind } from "./CanvasNode";
import { AddComponent } from "./AddComponent";
import { RemoveComponent } from "./RemoveComponent";

const CANVAS_W = 1600;
const CANVAS_H = 1000;
const RADIUS = 320;
/** Nodes closer than this are considered overlapping when auto-placing. */
const MIN_GAP = 130;

function layoutKey(path: string) {
  return `genesis:layout:${path}`;
}

/** Flatten a scaffold into orbit entries in a stable order. */
function orbitOf(scaffold: ScaffoldResult) {
  return [
    ...scaffold.neurons.map((n) => ({ kind: "neuron" as NodeKind, id: n.id, sublabel: n.file })),
    ...scaffold.engrams.map((e) => ({ kind: "engram" as NodeKind, id: e.id, sublabel: e.file })),
    ...scaffold.effectors.map((e) => ({ kind: "effector" as NodeKind, id: e.id, sublabel: e.file })),
    // Guarded: a scaffold read by an older backend has no receptors key.
    ...(scaffold.receptors ?? []).map((r) => ({ kind: "receptor" as NodeKind, id: r.id, sublabel: r.file })),
  ];
}

/** Evenly place nodes on a circle around the synapse at canvas center. */
function initialLayout(scaffold: ScaffoldResult): CanvasNodeData[] {
  const cx = CANVAS_W / 2;
  const cy = CANVAS_H / 2;
  const nodes: CanvasNodeData[] = [
    { key: "synapse", kind: "synapse", id: scaffold.synapse.id, x: cx, y: cy },
  ];

  const orbit = orbitOf(scaffold);
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

/**
 * Find a free spot on (or just outside) the orbit ring.
 *
 * Nodes the user has already dragged keep their saved position, so a fresh
 * component can't just take the ring slot its index implies - that slot is
 * often occupied. This walks the ring in 15-degree steps, widening the
 * radius each lap, and takes the first spot MIN_GAP clear of everything
 * placed so far.
 */
function freeSpot(placed: CanvasNodeData[]): { x: number; y: number } {
  const cx = CANVAS_W / 2;
  const cy = CANVAS_H / 2;
  for (let ring = 0; ring < 6; ring++) {
    const r = RADIUS + ring * 90;
    for (let step = 0; step < 24; step++) {
      const angle = (2 * Math.PI * step) / 24 - Math.PI / 2;
      const x = cx + r * Math.cos(angle);
      const y = cy + r * Math.sin(angle);
      const clear = placed.every((p) => Math.hypot(p.x - x, p.y - y) >= MIN_GAP);
      if (clear) return { x, y };
    }
  }
  return { x: cx + RADIUS, y: cy };
}

function loadLayout(scaffold: ScaffoldResult): CanvasNodeData[] {
  const fresh = initialLayout(scaffold);
  let saved: Record<string, { x: number; y: number }> = {};
  try {
    const raw = localStorage.getItem(layoutKey(scaffold.path));
    if (raw) saved = JSON.parse(raw);
  } catch {
    saved = {};
  }

  // Restore anything we've seen before, then drop the rest into whatever
  // space is left rather than letting them stack on a saved neighbour.
  const out: CanvasNodeData[] = [];
  const pending: CanvasNodeData[] = [];
  for (const n of fresh) {
    if (saved[n.key]) out.push({ ...n, ...saved[n.key] });
    else pending.push(n);
  }
  for (const n of pending) {
    if (n.kind === "synapse") {
      out.push(n);
      continue;
    }
    out.push({ ...n, ...freeSpot(out) });
  }
  // Back to scaffold order so the SVG paint order stays stable.
  const byKey = new Map(out.map((n) => [n.key, n]));
  return fresh.map((n) => byKey.get(n.key)!);
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

export { loadLayout };

/**
 * The draw.io-style layout surface: the project's Synapse at the centre,
 * every Neuron/Engram/Effector orbiting it, each drawn with the same
 * silhouette Prism gives it. Positions are per-project and persist in
 * localStorage.
 */
export function GenesisCanvas({
  scaffold,
  nodes,
  onNodes,
  onAdded,
  onRemoved,
}: {
  scaffold: ScaffoldResult;
  nodes: CanvasNodeData[];
  onNodes: (nodes: CanvasNodeData[]) => void;
  onAdded: (result: ComponentResult) => void;
  onRemoved: (result: RemoveResult) => void;
}) {
  const [selected, setSelected] = useState<string | null>(null);

  const edges = useMemo(() => {
    const synapse = nodes.find((n) => n.kind === "synapse");
    if (!synapse) return [];
    return nodes.filter((n) => n.kind !== "synapse").map((n) => ({ from: synapse, to: n }));
  }, [nodes]);

  function onDrag(key: string, x: number, y: number) {
    const next = nodes.map((n) =>
      n.key === key
        ? {
            ...n,
            x: Math.max(60, Math.min(CANVAS_W - 60, x)),
            y: Math.max(60, Math.min(CANVAS_H - 60, y)),
          }
        : n,
    );
    saveLayout(scaffold.path, next);
    onNodes(next);
  }

  const selectedNode = nodes.find((n) => n.key === selected) ?? null;
  // The synapse isn't a module, so there's nothing to archive; a node read
  // from an older backend can arrive without one either.
  const selectedFile = selectedNode
    ? fileOf(selectedNode.kind, selectedNode.sublabel)
    : null;

  return (
    <div style={{ flex: 1, position: "relative", overflow: "hidden" }}>
      <div style={{ position: "absolute", inset: 0, overflow: "auto" }}>
        <svg width={CANVAS_W} height={CANVAS_H} style={{ display: "block" }}>
          <CanvasDefs />

          {/* Ambient bloom behind the synapse, same as Prism's brain view */}
          <ellipse
            cx={CANVAS_W / 2}
            cy={CANVAS_H / 2}
            rx={320}
            ry={240}
            fill="url(#centerGlow)"
            filter="url(#blur-md)"
            style={{ pointerEvents: "none" }}
          />

          {edges.map((e) => (
            <line
              key={e.to.key}
              x1={e.from.x}
              y1={e.from.y}
              x2={e.to.x}
              y2={e.to.y}
              stroke={kindColor()[e.to.kind]}
              strokeOpacity={0.22}
              strokeWidth={1.2}
              style={{ pointerEvents: "none" }}
            />
          ))}

          {nodes.map((n) => (
            <CanvasNode
              key={n.key}
              node={n}
              onDrag={onDrag}
              onSelect={setSelected}
              selected={selected === n.key}
            />
          ))}
        </svg>
      </div>

      <AddComponent projectPath={scaffold.path} onAdded={onAdded} />
      <Legend />

      {selectedNode && (
        <div
          style={{
            position: "absolute",
            right: 20,
            bottom: 20,
            zIndex: 4,
            background: "var(--bg-panel)",
            WebkitBackdropFilter: "blur(20px)",
            backdropFilter: "blur(20px)",
            border: `1px solid ${C.borderStrong}`,
            borderRadius: 10,
            padding: "12px 16px",
            fontFamily: MONO,
            fontSize: 14.5,
            color: C.textDim, fontWeight: 600,
            maxWidth: 280,
          }}
        >
          <div style={{ color: kindColor()[selectedNode.kind], fontSize: 13, marginBottom: 4 }}>
            {selectedNode.kind}
          </div>
          <div style={{ color: C.text, marginBottom: 4 }}>{selectedNode.id}</div>
          {selectedNode.sublabel && (
            <div style={{ color: C.textFaint, fontWeight: 600, }}>{selectedNode.sublabel}</div>
          )}

          {/* Removal lives here rather than in the Add panel because it is
              about *this* node: you point at the thing you mean, and the
              confirm sentence can name it. */}
          {selectedFile && (
            <RemoveComponent
              key={selectedFile}
              projectPath={scaffold.path}
              file={selectedFile}
              label={selectedNode.id}
              accent={kindColor()[selectedNode.kind]}
              onRemoved={(r) => {
                setSelected(null);
                onRemoved(r);
              }}
            />
          )}
        </div>
      )}
    </div>
  );
}

/** Shape key - the silhouettes carry the meaning, so name them. */
function Legend() {
  const items: { kind: NodeKind; label: string }[] = [
    { kind: "neuron", label: "Neuron · thinks" },
    { kind: "engram", label: "Engram · remembers" },
    { kind: "effector", label: "Effector · acts" },
    { kind: "receptor", label: "Receptor · listens" },
  ];
  return (
    <div style={legendStyle}>
      {items.map((i) => (
        <div key={i.kind} style={{ display: "flex", alignItems: "center", gap: 7 }}>
          <svg width="12" height="12" viewBox="-10 -10 20 20">
            {i.kind === "neuron" && (
              <circle r="7" fill="none" stroke={kindColor()[i.kind]} strokeWidth="1.8" />
            )}
            {i.kind === "engram" && (
              <polygon
                points="0,-8 8,0 0,8 -8,0"
                fill="none"
                stroke={kindColor()[i.kind]}
                strokeWidth="1.8"
              />
            )}
            {i.kind === "effector" && (
              <polygon
                points="0,-8 6.93,4 -6.93,4"
                fill="none"
                stroke={kindColor()[i.kind]}
                strokeWidth="1.8"
              />
            )}
            {i.kind === "receptor" && (
              <path
                d={cup(8)}
                fill="none"
                stroke={kindColor()[i.kind]}
                strokeWidth="1.8"
              />
            )}
          </svg>
          {i.label}
        </div>
      ))}
    </div>
  );
}

const legendStyle: CSSProperties = {
  position: "absolute",
  right: 20,
  top: 20,
  zIndex: 4,
  display: "flex",
  flexDirection: "column",
  gap: 7,
  padding: "10px 14px",
  borderRadius: 10,
  background: "var(--bg-panel)",
  WebkitBackdropFilter: "blur(20px)",
  backdropFilter: "blur(20px)",
  border: "1px solid var(--border)",
  fontFamily: MONO,
  fontSize: 13.5,
  color: "var(--text-dim)",
  pointerEvents: "none",
};
