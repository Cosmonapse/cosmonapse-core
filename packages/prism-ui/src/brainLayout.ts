/**
 * Where every participant sits on the Brain View canvas, and how a signal
 * gets from one to another.
 *
 * Two arrangements of the same brain:
 *
 *   - "radial"  -  the synapse is a soma at the centre and participants ring
 *     it, receptors on an outer ring. Reads as an organism: one nucleus,
 *     everything else oriented toward it.
 *   - "bus"  -  the synapse is a horizontal bar, receptors above it and every
 *     other participant hanging below. Reads as an architecture diagram: the
 *     bus is a shared medium, and the bar is the line between the outside of
 *     the brain and the inside of it.
 *
 * Both produce the same three things, so the canvas renders either without
 * knowing which it got: a position per node id, the point where a node's edge
 * meets the synapse, and an SVG path for a travelling signal.
 */

import { SYNAPSE_NODE } from "./types";
import type { NeuronView } from "./types";

export type BrainLayout = "radial" | "bus";

export interface Point {
  x: number;
  y: number;
}

export interface BrainGeometry {
  mode: BrainLayout;
  /** Node id → centre point. Always contains SYNAPSE_NODE. */
  pos: Record<string, Point>;
  /** Where this node's axon meets the synapse. */
  junction(id: string): Point;
  /** Path for a signal `from` → (`via`) → `to`, or null if an end is unplaced. */
  route(from: string, via: string | undefined, to: string): string | null;
  /** Bus mode only: the bar, for rendering. */
  bar: { y: number; x0: number; x1: number } | null;
  /** Nodes drawn above the synapse want their labels above them too. */
  labelAbove(id: string): boolean;
}

/* ─────────────────────────────  persistence  ───────────────────────────── */

const KEY = "cosmonapse.prism.brain-layout.v1";

export function readStoredLayout(): BrainLayout {
  try {
    return localStorage.getItem(KEY) === "bus" ? "bus" : "radial";
  } catch {
    return "radial";
  }
}

export function storeLayout(mode: BrainLayout): void {
  try {
    localStorage.setItem(KEY, mode);
  } catch {
    /* private mode - the choice still holds for this session */
  }
}

/* ───────────────────────────────  paths  ──────────────────────────────── */

const dist = (a: Point, b: Point) => Math.hypot(b.x - a.x, b.y - a.y);

/** Quadratic through the midpoint - a straight line, kept for radial parity. */
function quad(from: Point, to: Point): string {
  return `M${from.x} ${from.y} Q${(from.x + to.x) / 2} ${(from.y + to.y) / 2},${to.x} ${to.y}`;
}

/**
 * A polyline with the corners rounded off, so a signal turning onto the bus
 * sweeps through the corner instead of snapping through it. Duplicate points
 * are dropped first: a node sitting directly over the bar centre would
 * otherwise produce a zero-length segment and a NaN in the path.
 */
function roundedPoly(raw: Point[], r: number): string {
  const pts: Point[] = [];
  for (const p of raw) {
    const last = pts[pts.length - 1];
    if (!last || dist(last, p) > 0.5) pts.push(p);
  }
  if (pts.length < 2) return "";
  let d = `M${pts[0].x} ${pts[0].y}`;
  for (let i = 1; i < pts.length - 1; i++) {
    const p = pts[i];
    const a = pts[i - 1];
    const b = pts[i + 1];
    const da = Math.min(r, dist(a, p) / 2);
    const db = Math.min(r, dist(b, p) / 2);
    const ua = { x: (a.x - p.x) / dist(a, p), y: (a.y - p.y) / dist(a, p) };
    const ub = { x: (b.x - p.x) / dist(b, p), y: (b.y - p.y) / dist(b, p) };
    d += ` L${p.x + ua.x * da} ${p.y + ua.y * da}`;
    d += ` Q${p.x} ${p.y} ${p.x + ub.x * db} ${p.y + ub.y * db}`;
  }
  const end = pts[pts.length - 1];
  d += ` L${end.x} ${end.y}`;
  return d;
}

/* ──────────────────────────────  radial  ──────────────────────────────── */

function radial(all: NeuronView[], vp: { w: number; h: number }): BrainGeometry {
  const cx = vp.w / 2;
  const cy = vp.h / 2;
  const baseR = Math.max(180, Math.min(vp.w, vp.h) * 0.32);
  // Receptors are the boundary of the brain, so they get their own ring
  // outside every neuron rather than a slot among them. Placing them on the
  // shared ring would put the edge of the system in the middle of it.
  const inner = all.filter((ne) => ne.kind !== "receptor");
  const edge = all.filter((ne) => ne.kind === "receptor");
  const pos: Record<string, Point> = {};

  const n = Math.max(inner.length, 1);
  inner.forEach((ne, i) => {
    const a = (i / n) * Math.PI * 2 - Math.PI / 2;
    const ring = Math.floor(i / 12);
    pos[ne.id] = { x: cx + Math.cos(a) * (baseR + ring * 70), y: cy + Math.sin(a) * (baseR + ring * 70) };
  });

  const m = Math.max(edge.length, 1);
  const edgeR = baseR + 150;
  edge.forEach((ne, i) => {
    // Offset by half a slot so a lone receptor doesn't sit directly on the
    // radius of the first neuron and hide the axon line behind it.
    const a = ((i + 0.5) / m) * Math.PI * 2 - Math.PI / 2;
    const ring = Math.floor(i / 12);
    pos[ne.id] = { x: cx + Math.cos(a) * (edgeR + ring * 70), y: cy + Math.sin(a) * (edgeR + ring * 70) };
  });

  const soma = { x: cx, y: cy };
  pos[SYNAPSE_NODE] = soma;

  return {
    mode: "radial",
    pos,
    bar: null,
    junction: () => soma,
    labelAbove: () => false,
    route(from, via, to) {
      const a = pos[from];
      const b = pos[to];
      if (!a || !b) return null;
      if (!via) return quad(a, b);
      const v = pos[via];
      if (!v) return quad(a, b);
      return (
        `M${a.x} ${a.y} Q${(a.x + v.x) / 2} ${(a.y + v.y) / 2},${v.x} ${v.y}` +
        ` Q${(v.x + b.x) / 2} ${(v.y + b.y) / 2},${b.x} ${b.y}`
      );
    },
  };
}

/* ────────────────────────────────  bus  ───────────────────────────────── */

const BAR_INSET = 56;
/** Enough room for the widest label plus breathing space. */
const MIN_SLOT = 156;
const ROW_GAP = 132;
/** Clearance between the bar and the first row on either side of it. */
const DROP_BELOW = 150;
const DROP_ABOVE = 138;
const CORNER = 16;

/** Even slots across [x0,x1], wrapping into rows once they'd get too tight. */
function lane(items: NeuronView[], x0: number, x1: number, y0: number, dir: 1 | -1, pos: Record<string, Point>) {
  const width = Math.max(x1 - x0, MIN_SLOT);
  const perRow = Math.max(1, Math.floor(width / MIN_SLOT));
  items.forEach((ne, i) => {
    const row = Math.floor(i / perRow);
    const inRow = i % perRow;
    // The last row is usually short; spreading it over its own count keeps it
    // centred under the bar instead of bunched at the left edge.
    const count = Math.min(perRow, items.length - row * perRow);
    const slot = width / count;
    pos[ne.id] = { x: x0 + slot * (inRow + 0.5), y: y0 + dir * row * ROW_GAP };
  });
}

function bus(all: NeuronView[], vp: { w: number; h: number }, offset: number): BrainGeometry {
  // The sidebar overlays the right of the canvas, so the bar stops short of
  // it rather than sliding underneath.
  const W = Math.max(vp.w - offset, 420);
  const x0 = BAR_INSET;
  const x1 = W - BAR_INSET;

  const inner = all.filter((ne) => ne.kind !== "receptor");
  const edge = all.filter((ne) => ne.kind === "receptor");

  // With nothing above it the bar rides high and gives the whole canvas to
  // the participants; receptors push it back down to make room for them.
  const barY = edge.length ? Math.max(vp.h * 0.34, DROP_ABOVE + 60) : vp.h * 0.22;

  const pos: Record<string, Point> = {};
  lane(inner, x0, x1, barY + DROP_BELOW, 1, pos);
  lane(edge, x0, x1, barY - DROP_ABOVE, -1, pos);

  const centre = { x: (x0 + x1) / 2, y: barY };
  pos[SYNAPSE_NODE] = centre;

  const clamp = (x: number) => Math.min(Math.max(x, x0 + 10), x1 - 10);
  const junction = (id: string): Point => {
    const p = pos[id];
    return p ? { x: clamp(p.x), y: barY } : centre;
  };
  const above = new Set(edge.map((ne) => ne.id));

  return {
    mode: "bus",
    pos,
    bar: { y: barY, x0, x1 },
    junction,
    labelAbove: (id) => above.has(id),
    route(from, via, to) {
      const a = pos[from];
      const b = pos[to];
      if (!a || !b) return null;
      // Every leg is the same shape: leave the node vertically, ride the bar,
      // drop onto the destination. A leg that starts or ends *at* the synapse
      // uses the bar's centre as its stub, so it still visibly travels.
      const pts: Point[] = [];
      if (from === SYNAPSE_NODE) {
        pts.push(centre, junction(to), b);
      } else if (to === SYNAPSE_NODE) {
        pts.push(a, junction(from), centre);
      } else if (via === SYNAPSE_NODE) {
        pts.push(a, junction(from), junction(to), b);
      } else {
        pts.push(a, junction(from), junction(to), b);
      }
      return roundedPoly(pts, CORNER);
    },
  };
}

/* ────────────────────────────────  entry  ─────────────────────────────── */

export function brainGeometry(
  neurons: Map<string, NeuronView>,
  vp: { w: number; h: number },
  mode: BrainLayout,
  sidebarOffset: number,
): BrainGeometry {
  const all = Array.from(neurons.values());
  return mode === "bus" ? bus(all, vp, sidebarOffset) : radial(all, vp);
}
