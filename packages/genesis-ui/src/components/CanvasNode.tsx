import { useRef } from "react";
import { C, MONO } from "../theme";

export type NodeKind = "synapse" | "neuron" | "effector" | "engram" | "receptor";

// Read through a call, not a frozen object: these feed SVG presentation
// attributes, which cannot resolve var(), so they must re-read the live
// palette on every render.
export const kindColor = (): Record<NodeKind, string> => ({
  synapse: C.synapse,
  neuron: C.neuron,
  effector: C.effector,
  engram: C.engram,
  receptor: C.receptor,
});

/**
 * Which package each primitive's modules live in.
 *
 * Here rather than in either of its callers because both the Add panel (which
 * predicts the path a new module will take) and the removal control (which
 * has to name an existing one) need the same answer, and two copies of it is
 * how the two end up disagreeing.
 */
export const KIND_FOLDER: Record<Exclude<NodeKind, "synapse">, string> = {
  neuron: "neurons",
  effector: "effector",
  engram: "engram",
  receptor: "receptors",
};

/** A canvas node's project-relative module path; null for the synapse, which
 *  is the project itself and has no module of its own. */
export function fileOf(kind: NodeKind, sublabel: string | undefined): string | null {
  if (kind === "synapse" || !sublabel) return null;
  return `${KIND_FOLDER[kind]}/${sublabel}`;
}

export interface CanvasNodeData {
  key: string;
  kind: NodeKind;
  id: string;
  /** Path of the module this node came from, relative to the project root. */
  sublabel?: string;
  x: number;
  y: number;
}

/**
 * A draggable node on the Genesis canvas, drawn with the *same geometry as
 * Prism* (packages/prism-ui/src/components/PrismCanvas.tsx):
 *
 *     synapse   →  soma      (the concentric ringed core at the centre)
 *     neuron    →  circle    (axon-backed participant - Neurons think)
 *     engram    →  diamond   (Engram memory backend - Engrams remember)
 *     effector  →  triangle  (Effector tool backend - Effectors act)
 *     receptor  →  cup       (the listening edge - Receptors listen)
 *
 * A component keeps one silhouette across the whole product: you lay it out
 * in Genesis and then watch that exact shape light up in Prism.
 *
 * Dragging is plain pointer events on the <g> (no library, matching
 * prism-ui's minimal footprint) - pointer down captures, move reports the
 * new x/y in canvas coordinates, up releases. The canvas is rendered at 1:1
 * inside a scrolling viewport, so client deltas are canvas deltas.
 */
export function CanvasNode({
  node,
  onDrag,
  onSelect,
  selected,
}: {
  node: CanvasNodeData;
  onDrag: (key: string, x: number, y: number) => void;
  onSelect: (key: string) => void;
  selected: boolean;
}) {
  const dragRef = useRef<{
    startX: number;
    startY: number;
    nodeX: number;
    nodeY: number;
  } | null>(null);

  function onPointerDown(e: React.PointerEvent) {
    (e.currentTarget as Element).setPointerCapture(e.pointerId);
    dragRef.current = {
      startX: e.clientX,
      startY: e.clientY,
      nodeX: node.x,
      nodeY: node.y,
    };
    onSelect(node.key);
  }

  function onPointerMove(e: React.PointerEvent) {
    const d = dragRef.current;
    if (!d) return;
    onDrag(node.key, d.nodeX + (e.clientX - d.startX), d.nodeY + (e.clientY - d.startY));
  }

  function onPointerUp(e: React.PointerEvent) {
    if (dragRef.current) {
      (e.currentTarget as Element).releasePointerCapture(e.pointerId);
      dragRef.current = null;
    }
  }

  const handlers = {
    onPointerDown,
    onPointerMove,
    onPointerUp,
    onPointerCancel: onPointerUp,
    style: { cursor: "grab", touchAction: "none" as const },
  };

  return (
    <g transform={`translate(${node.x},${node.y})`} {...handlers}>
      {node.kind === "synapse" && <SynapseShape selected={selected} />}
      {node.kind === "neuron" && <NeuronShape selected={selected} />}
      {node.kind === "engram" && <EngramShape selected={selected} />}
      {node.kind === "effector" && <EffectorShape selected={selected} />}
      {node.kind === "receptor" && <ReceptorShape selected={selected} />}
      <Label node={node} />
    </g>
  );
}

// ── labels ────────────────────────────────────────────────────────────────
// Offsets are per-kind so the text always clears the widest part of the
// silhouette (a triangle's base sits lower than a circle's edge).
const LABEL_Y: Record<NodeKind, number> = {
  synapse: 62,
  neuron: 40,
  engram: 44,
  effector: 44,
  // The bowl hangs below the origin and the waves rise above it, so the
  // label needs to clear the floor of the cup rather than a centred body.
  receptor: 46,
};

function Label({ node }: { node: CanvasNodeData }) {
  const y = LABEL_Y[node.kind];
  const color = kindColor()[node.kind];
  const id = node.id.length > 20 ? node.id.slice(0, 18) + "…" : node.id;
  return (
    <>
      <text
        y={y}
        textAnchor="middle"
        fontSize="14.5"
        fontWeight="500"
        fill={C.text}
        style={{ fontFamily: MONO, pointerEvents: "none", userSelect: "none" }}
      >
        {id}
      </text>
      {node.sublabel && (
        <text
          y={y + 14}
          textAnchor="middle"
          fontSize="12"
          fill={node.kind === "neuron" ? C.textFaint : color}
          style={{ fontFamily: MONO, pointerEvents: "none", userSelect: "none" }}
        >
          {node.sublabel}
        </text>
      )}
    </>
  );
}

// ── selection ring ────────────────────────────────────────────────────────
// A dashed marching-ants outline drawn outside the body, so selecting never
// alters the silhouette itself.
function SelectRing({ r, color, shape = "circle" }: {
  r: number;
  color: string;
  shape?: "circle" | "diamond" | "triangle" | "cup";
}) {
  const dash = { strokeDasharray: "4 4" };
  if (shape === "cup") {
    return (
      <path
        d={cup(r)}
        fill="none"
        stroke={color}
        strokeWidth="1.2"
        strokeOpacity="0.9"
        style={{ ...dash, pointerEvents: "none" }}
      />
    );
  }
  if (shape === "diamond") {
    return (
      <polygon
        points={`0,${-r} ${r},0 0,${r} ${-r},0`}
        fill="none"
        stroke={color}
        strokeWidth="1.2"
        strokeOpacity="0.9"
        style={{ ...dash, pointerEvents: "none" }}
      />
    );
  }
  if (shape === "triangle") {
    const dx = r * 0.8660254;
    const dy = r * 0.5;
    return (
      <polygon
        points={`0,${-r} ${dx},${dy} ${-dx},${dy}`}
        fill="none"
        stroke={color}
        strokeWidth="1.2"
        strokeOpacity="0.9"
        style={{ ...dash, pointerEvents: "none" }}
      />
    );
  }
  return (
    <circle
      r={r}
      fill="none"
      stroke={color}
      strokeWidth="1.2"
      strokeOpacity="0.9"
      style={{ ...dash, pointerEvents: "none" }}
    />
  );
}

// ── synapse soma ──────────────────────────────────────────────────────────
// Prism's SynapseNode at R=56, scaled to 40 so it doesn't swallow the
// Genesis canvas.
function SynapseShape({ selected }: { selected: boolean }) {
  const R = 40;
  return (
    <>
      <circle r={R * 3.2} fill="none" stroke={C.accent} strokeOpacity="0.07" strokeWidth="1" filter="url(#blur-md)" />
      <circle r={R * 2.2} fill={C.accent} fillOpacity="0.06" filter="url(#blur-md)" />
      <circle r={R} fill="url(#somaFill)" stroke={C.accent2} strokeWidth="1.5" strokeOpacity="0.7"
        style={{ filter: `drop-shadow(0 0 14px ${C.accent})` }} />
      <circle r={R * 0.72} fill="none" stroke={C.accent2} strokeWidth="0.8" strokeOpacity="0.22" />
      <circle r={R * 0.48} fill="none" stroke={C.accent} strokeWidth="0.8" strokeOpacity="0.3" />
      <circle r={R * 0.34} fill="none" stroke={C.accent2} strokeWidth="1.5" strokeOpacity="0.6">
        <animateTransform attributeName="transform" type="rotate" from="0" to="360" dur="9s" repeatCount="indefinite" />
      </circle>
      <circle r={R * 0.2} fill={C.accent2} fillOpacity="0.85" filter="url(#glow-soft)">
        <animate attributeName="r" values={`${R * 0.16};${R * 0.24};${R * 0.16}`} dur="2.6s" repeatCount="indefinite" />
      </circle>
      {selected && <SelectRing r={R * 1.3} color={C.synapse} />}
    </>
  );
}

// ── neuron (circle) ───────────────────────────────────────────────────────
function NeuronShape({ selected }: { selected: boolean }) {
  const R = 22;
  const color = C.neuron;
  return (
    <>
      <circle r={R * 2.8} fill={color} fillOpacity="0.07" filter="url(#blur-md)" />
      <circle r={R * 1.35} fill="none" stroke={color} strokeOpacity="0.2" strokeWidth="0.8" />
      <circle r={R} fill={C.bg} stroke={color} strokeWidth="1.5"
        style={{ filter: `drop-shadow(0 0 7px ${color})` }} />
      <circle r={R * 0.6} fill={color} fillOpacity="0.12" />
      <circle r={R * 0.32} fill={C.accent3} fillOpacity="0.75" filter="url(#glow-soft)">
        <animate attributeName="r" values={`${R * 0.28};${R * 0.38};${R * 0.28}`} dur="2.4s" repeatCount="indefinite" />
      </circle>
      {selected && <SelectRing r={R * 1.6} color={color} />}
    </>
  );
}

// ── engram (diamond) ──────────────────────────────────────────────────────
function EngramShape({ selected }: { selected: boolean }) {
  const R = 22;
  const color = C.engram;
  const D = R * 1.22;
  const pts = `0,${-D} ${D},0 0,${D} ${-D},0`;
  return (
    <>
      <polygon points={`0,${-D * 2.6} ${D * 2.6},0 0,${D * 2.6} ${-D * 2.6},0`}
        fill={color} fillOpacity="0.06" filter="url(#blur-md)" />
      <polygon points={`0,${-D * 1.4} ${D * 1.4},0 0,${D * 1.4} ${-D * 1.4},0`}
        fill="none" stroke={color} strokeOpacity="0.2" strokeWidth="0.8" />
      <polygon points={pts} fill={C.bg} stroke={color} strokeWidth="1.5"
        style={{ filter: `drop-shadow(0 0 7px ${color})` }} />
      <polygon points={`0,${-D * 0.6} ${D * 0.6},0 0,${D * 0.6} ${-D * 0.6},0`}
        fill={color} fillOpacity="0.13" />
      <polygon points={`0,${-D * 0.38} ${D * 0.38},0 0,${D * 0.38} ${-D * 0.38},0`}
        fill="none" stroke={color} strokeWidth="1" strokeOpacity="0.55">
        <animateTransform attributeName="transform" type="rotate" from="45" to="405" dur="8s" repeatCount="indefinite" />
      </polygon>
      <circle r={R * 0.28} fill={color} fillOpacity="0.7" filter="url(#glow-soft)">
        <animate attributeName="r" values={`${R * 0.22};${R * 0.34};${R * 0.22}`} dur="3.2s" repeatCount="indefinite" />
      </circle>
      {selected && <SelectRing r={D * 1.7} color={color} shape="diamond" />}
    </>
  );
}

// ── effector (triangle) ───────────────────────────────────────────────────
function EffectorShape({ selected }: { selected: boolean }) {
  const R = 22;
  const color = C.effector;
  // Upward-pointing equilateral triangle inscribed in radius r.
  const tri = (r: number) => {
    const dx = r * 0.8660254; // cos(30deg)
    const dy = r * 0.5;       // sin(30deg)
    return `0,${-r} ${dx},${dy} ${-dx},${dy}`;
  };
  return (
    <>
      <polygon points={tri(R * 2.6)} fill={color} fillOpacity="0.06" filter="url(#blur-md)" />
      <polygon points={tri(R * 1.55)} fill="none" stroke={color} strokeOpacity="0.2" strokeWidth="0.8" />
      <polygon points={tri(R * 1.22)} fill={C.bg} stroke={color} strokeWidth="1.5"
        style={{ filter: `drop-shadow(0 0 7px ${color})` }} />
      <polygon points={tri(R * 0.7)} fill={color} fillOpacity="0.13" />
      <polygon points={tri(R * 0.44)} fill="none" stroke={color} strokeWidth="1" strokeOpacity="0.55">
        <animateTransform attributeName="transform" type="rotate" from="0" to="360" dur="6s" repeatCount="indefinite" />
      </polygon>
      <circle cy={R * 0.14} r={R * 0.28} fill={color} fillOpacity="0.7" filter="url(#glow-soft)">
        <animate attributeName="r" values={`${R * 0.22};${R * 0.34};${R * 0.22}`} dur="2.8s" repeatCount="indefinite" />
      </circle>
      {selected && <SelectRing r={R * 1.9} color={color} shape="triangle" />}
    </>
  );
}

// ── receptor (cup) ────────────────────────────────────────────────────────
// A hollow bowl - the annular lower half between r and 0.62r - with the mouth
// opening away from the synapse. Biologically a receptor *is* a pocket a
// ligand binds to, and the same outline reads as a dish antenna: the one
// silhouette in the set that is open rather than closed, because a Receptor
// is the only primitive that faces outward.
export const cup = (r: number) => {
  const i = r * 0.62;
  return `M ${-r},0 A ${r},${r} 0 0 0 ${r},0 L ${i},0 A ${i},${i} 0 0 1 ${-i},0 Z`;
};

// The arcs above the mouth - incoming traffic being received. This is the
// receptor's analogue of the effector's rotating inner ring: motion that says
// what the primitive does.
export const wave = (r: number) => `M ${-r},0 A ${r},${r} 0 0 1 ${r},0`;

function ReceptorShape({ selected }: { selected: boolean }) {
  const R = 22;
  const color = C.receptor;
  return (
    <>
      <path d={cup(R * 2.4)} fill={color} fillOpacity="0.06" filter="url(#blur-md)" />
      {/* Reception arcs, staggered so they read as arriving */}
      {[1.5, 1.95].map((k, i) => (
        <path key={k} d={wave(R * k)} fill="none" stroke={color} strokeWidth="1" strokeOpacity="0.28">
          <animate
            attributeName="stroke-opacity"
            values="0.05;0.4;0.05"
            dur="2.8s"
            begin={`${i * 0.5}s`}
            repeatCount="indefinite"
          />
        </path>
      ))}
      <path d={cup(R * 1.55)} fill="none" stroke={color} strokeOpacity="0.2" strokeWidth="0.8" />
      <path d={cup(R * 1.22)} fill={C.bg} stroke={color} strokeWidth="1.5"
        style={{ filter: `drop-shadow(0 0 7px ${color})` }} />
      {/* The bound ligand resting in the pocket */}
      <circle cy={R * 0.52} r={R * 0.26} fill={color} fillOpacity="0.75" filter="url(#glow-soft)">
        <animate attributeName="r" values={`${R * 0.2};${R * 0.32};${R * 0.2}`} dur="2.6s" repeatCount="indefinite" />
      </circle>
      {selected && <SelectRing r={R * 1.85} color={color} shape="cup" />}
    </>
  );
}

/** Shared <defs> (gradients + blur filters) the shapes above reference. */
export function CanvasDefs() {
  return (
    <defs>
      <radialGradient id="somaFill" cx="50%" cy="50%" r="50%">
        <stop offset="0%" stopColor={C.accent2} stopOpacity="0.55" />
        <stop offset="40%" stopColor={C.accent} stopOpacity="0.3" />
        <stop offset="100%" stopColor={C.bg} stopOpacity="1" />
      </radialGradient>
      <radialGradient id="centerGlow" cx="50%" cy="50%" r="50%">
        <stop offset="0%" stopColor={C.accent} stopOpacity="0.22" />
        <stop offset="55%" stopColor={C.accent2} stopOpacity="0.08" />
        <stop offset="100%" stopColor={C.accent} stopOpacity="0" />
      </radialGradient>
      <filter id="blur-sm"><feGaussianBlur stdDeviation="2" /></filter>
      <filter id="blur-md"><feGaussianBlur stdDeviation="5" /></filter>
      <filter id="glow-soft"><feGaussianBlur stdDeviation="3" /></filter>
    </defs>
  );
}
