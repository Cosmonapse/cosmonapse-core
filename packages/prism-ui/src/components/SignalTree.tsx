import { useMemo, useState } from "react";
import { C, MONO, colorFor } from "../theme";
import { groupSignals, ts, type TaskGroup } from "../grouping";
import type { Signal } from "../types";

interface Props {
  signals: Signal[];
}

interface SigNode {
  sig: Signal;
  children: SigNode[];
}
interface LNode extends SigNode {
  children: LNode[];
  x: number;
  y: number;
}

const R = 21; // node radius
const SPACING_X = 92;
const SPACING_Y = 108;
const MARGIN_X = 56;
const MARGIN_Y = 44;

type LayoutMode = "simple" | "rt";

/** All signals of a task and its nested sub-tasks, deduped. */
function subtreeSignals(task: TaskGroup): Signal[] {
  const out: Signal[] = [];
  const walk = (t: TaskGroup) => {
    for (const pw of t.pathways) out.push(...pw.signals);
    t.children.forEach(walk);
  };
  walk(task);
  const seen = new Set<string>();
  const res: Signal[] = [];
  for (const s of out) {
    const k = s.id || `${s.trace_id}:${s.ts}`;
    if (seen.has(k)) continue;
    seen.add(k);
    res.push(s);
  }
  return res;
}

function buildSigTree(sigs: Signal[]): SigNode[] {
  const byId = new Map<string, Signal>();
  for (const s of sigs) if (s.id) byId.set(s.id, s);
  const childrenOf = new Map<string, Signal[]>();
  const roots: Signal[] = [];
  for (const s of sigs) {
    const p = s.parent_id;
    if (p && byId.has(p) && p !== s.id) {
      const arr = childrenOf.get(p) ?? [];
      arr.push(s);
      childrenOf.set(p, arr);
    } else {
      roots.push(s);
    }
  }
  const build = (s: Signal): SigNode => ({
    sig: s,
    children: (childrenOf.get(s.id) ?? []).sort((a, b) => ts(a) - ts(b)).map(build),
  });
  return roots.sort((a, b) => ts(a) - ts(b)).map(build);
}

/** Simple layout: leaves get sequential x, parents centre over direct children.
 *  Fast and non-overlapping, but a parent sits over its children's midpoint,
 *  not its full leaf span, so lopsided trees look skewed. */
function layoutSimple(roots: SigNode[]): { lroots: LNode[]; width: number; height: number } {
  let nextX = 0;
  let maxDepth = 0;
  const place = (n: SigNode, depth: number): LNode => {
    maxDepth = Math.max(maxDepth, depth);
    const children = n.children.map((c) => place(c, depth + 1));
    let x: number;
    if (children.length === 0) {
      x = nextX * SPACING_X;
      nextX += 1;
    } else {
      x = (children[0].x + children[children.length - 1].x) / 2;
    }
    return { sig: n.sig, children, x, y: depth * SPACING_Y };
  };
  const lroots = roots.map((r) => place(r, 0));
  const width = Math.max(1, nextX) * SPACING_X;
  const height = (maxDepth + 1) * SPACING_Y;
  return { lroots, width, height };
}

// ── Reingold–Tilford (Buchheim, Jünger & Leipert 2002) — O(n) tidy tree ──
interface RT {
  sig: Signal;
  children: RT[];
  parent: RT | null;
  number: number; // 1-based index among siblings
  depth: number;
  prelim: number;
  mod: number;
  shift: number;
  change: number;
  thread: RT | null;
  ancestor: RT;
  x: number;
}

function layoutRT(roots: SigNode[]): { lroots: LNode[]; width: number; height: number } {
  const D = SPACING_X;

  const toRT = (n: SigNode, parent: RT | null, number: number, depth: number): RT => {
    const node: RT = {
      sig: n.sig, children: [], parent, number, depth,
      prelim: 0, mod: 0, shift: 0, change: 0, thread: null,
      ancestor: null as unknown as RT, x: 0,
    };
    node.ancestor = node;
    node.children = n.children.map((c, i) => toRT(c, node, i + 1, depth + 1));
    return node;
  };
  // virtual super-root so a forest of roots is separated correctly
  const root: RT = {
    sig: undefined as unknown as Signal, children: [], parent: null, number: 1, depth: 0,
    prelim: 0, mod: 0, shift: 0, change: 0, thread: null, ancestor: null as unknown as RT, x: 0,
  };
  root.ancestor = root;
  root.children = roots.map((r, i) => toRT(r, root, i + 1, 1));

  const leftSibling = (v: RT): RT | null => {
    if (!v.parent) return null;
    return v.number > 1 ? v.parent.children[v.number - 2] : null;
  };
  const nextLeft = (v: RT): RT | null => (v.children.length ? v.children[0] : v.thread);
  const nextRight = (v: RT): RT | null => (v.children.length ? v.children[v.children.length - 1] : v.thread);

  const moveSubtree = (wm: RT, wp: RT, shift: number) => {
    const subtrees = wp.number - wm.number;
    wp.change -= shift / subtrees;
    wp.shift += shift;
    wm.change += shift / subtrees;
    wp.prelim += shift;
    wp.mod += shift;
  };
  const executeShifts = (v: RT) => {
    let shift = 0;
    let change = 0;
    for (let i = v.children.length - 1; i >= 0; i--) {
      const w = v.children[i];
      w.prelim += shift;
      w.mod += shift;
      change += w.change;
      shift += w.shift + change;
    }
  };
  const anc = (vim: RT, v: RT, defaultAncestor: RT): RT =>
    v.parent && vim.ancestor.parent === v.parent ? vim.ancestor : defaultAncestor;

  const apportion = (v: RT, defaultAncestor: RT): RT => {
    const w = leftSibling(v);
    if (!w) return defaultAncestor;
    let vip: RT = v;
    let vop: RT = v;
    let vim: RT = w;
    let vom: RT = v.parent!.children[0];
    let sip = vip.mod;
    let sop = vop.mod;
    let sim = vim.mod;
    let som = vom.mod;
    let nr = nextRight(vim);
    let nl = nextLeft(vip);
    while (nr && nl) {
      vim = nr;
      vip = nl;
      vom = nextLeft(vom)!;
      vop = nextRight(vop)!;
      vop.ancestor = v;
      const shift = vim.prelim + sim - (vip.prelim + sip) + D;
      if (shift > 0) {
        moveSubtree(anc(vim, v, defaultAncestor), v, shift);
        sip += shift;
        sop += shift;
      }
      sim += vim.mod;
      sip += vip.mod;
      som += vom.mod;
      sop += vop.mod;
      nr = nextRight(vim);
      nl = nextLeft(vip);
    }
    if (nextRight(vim) && !nextRight(vop)) {
      vop.thread = nextRight(vim);
      vop.mod += sim - sop;
    }
    if (nextLeft(vip) && !nextLeft(vom)) {
      vom.thread = nextLeft(vip);
      vom.mod += sip - som;
      defaultAncestor = v;
    }
    return defaultAncestor;
  };

  const firstWalk = (v: RT) => {
    if (v.children.length === 0) {
      const w = leftSibling(v);
      v.prelim = w ? w.prelim + D : 0;
    } else {
      let defaultAncestor = v.children[0];
      for (const c of v.children) {
        firstWalk(c);
        defaultAncestor = apportion(c, defaultAncestor);
      }
      executeShifts(v);
      const midpoint = (v.children[0].prelim + v.children[v.children.length - 1].prelim) / 2;
      const w = leftSibling(v);
      if (w) {
        v.prelim = w.prelim + D;
        v.mod = v.prelim - midpoint;
      } else {
        v.prelim = midpoint;
      }
    }
  };

  const real: RT[] = [];
  const secondWalk = (v: RT, m: number) => {
    v.x = v.prelim + m;
    if (v.depth >= 1) real.push(v);
    for (const c of v.children) secondWalk(c, m + v.mod);
  };

  firstWalk(root);
  secondWalk(root, 0);

  if (real.length === 0) return { lroots: [], width: 1, height: 1 };
  let minX = Infinity;
  let maxX = -Infinity;
  let maxDepth = 0;
  for (const n of real) {
    if (n.x < minX) minX = n.x;
    if (n.x > maxX) maxX = n.x;
    if (n.depth > maxDepth) maxDepth = n.depth;
  }
  const toLNode = (rt: RT): LNode => ({
    sig: rt.sig,
    x: rt.x - minX,
    y: (rt.depth - 1) * SPACING_Y,
    children: rt.children.map(toLNode),
  });
  const lroots = root.children.map(toLNode);
  return { lroots, width: Math.max(1, maxX - minX), height: maxDepth * SPACING_Y };
}

/** Balance pass for the tidy layout: centre each internal node over its full
 *  subtree extent, then enforce the minimum gap per level with a left-to-right
 *  push (order-preserving, push-right-only) so no two nodes can overlap. RT
 *  already guarantees leaf spacing, so leaves never move; only recentred
 *  parents are nudged, and only ever rightward. */
function centerParents(roots: LNode[]): void {
  const extent = (n: LNode): { min: number; max: number } => {
    if (n.children.length === 0) return { min: n.x, max: n.x };
    let mn = Infinity;
    let mx = -Infinity;
    for (const c of n.children) {
      const r = extent(c);
      if (r.min < mn) mn = r.min;
      if (r.max > mx) mx = r.max;
    }
    n.x = (mn + mx) / 2;
    return { min: mn, max: mx };
  };
  roots.forEach(extent);
  const levels = new Map<number, LNode[]>();
  const collect = (n: LNode) => {
    const a = levels.get(n.y) ?? [];
    a.push(n);
    levels.set(n.y, a);
    n.children.forEach(collect);
  };
  roots.forEach(collect);
  for (const arr of levels.values()) {
    arr.sort((a, b) => a.x - b.x);
    for (let i = 1; i < arr.length; i++) {
      if (arr[i].x < arr[i - 1].x + SPACING_X) arr[i].x = arr[i - 1].x + SPACING_X;
    }
  }
}

function abbr(type: string): string {
  return type
    .split("_")
    .map((w) => w[0])
    .join("")
    .slice(0, 3)
    .toUpperCase();
}

export function SignalTree({ signals }: Props) {
  const grouped = useMemo(() => groupSignals(signals), [signals]);
  const roots = grouped.roots;
  const [selTrace, setSelTrace] = useState<string | null>(null);
  const [selSigId, setSelSigId] = useState<string | null>(null);
  const [full, setFull] = useState(false);
  const [mode, setMode] = useState<LayoutMode>("rt");

  const activeTask = roots.find((r) => r.trace === selTrace) ?? roots[0] ?? null;

  const { width, height, nodes, edges } = useMemo(() => {
    if (!activeTask) return { width: 1, height: 1, nodes: [] as LNode[], edges: [] as any[] };
    const tree = buildSigTree(subtreeSignals(activeTask));
    const laid = mode === "rt" ? layoutRT(tree) : layoutSimple(tree);
    if (mode === "rt") centerParents(laid.lroots);
    const nodes: LNode[] = [];
    const edges: { x1: number; y1: number; x2: number; y2: number; color: string }[] = [];
    const walk = (n: LNode) => {
      nodes.push(n);
      for (const c of n.children) {
        edges.push({ x1: n.x, y1: n.y + R, x2: c.x, y2: c.y - R, color: colorFor(c.sig.type) });
        walk(c);
      }
    };
    laid.lroots.forEach(walk);
    const maxX = nodes.reduce((m, n) => Math.max(m, n.x), 0);
    return { width: Math.max(laid.width, maxX), height: laid.height, nodes, edges };
  }, [activeTask, mode]);

  const selSig = useMemo(
    () => nodes.find((n) => n.sig.id === selSigId)?.sig ?? null,
    [nodes, selSigId],
  );

  const svgW = width + MARGIN_X * 2;
  const svgH = height + MARGIN_Y * 2;

  return (
    <div
      style={{
        position: "absolute",
        top: 64,
        left: 0,
        right: 0,
        bottom: 0,
        display: "flex",
        flexDirection: "row",
        background: "var(--bg-view)",
      }}
    >
      {/* left: parent task list ("files") */}
        <div
          style={{
            width: 244,
            flexShrink: 0,
            borderRight: "1px solid " + C.border,
            overflowY: "auto",
            background: "var(--bg-rail)",
          }}
        >
          <div
            style={{
              padding: "12px 14px",
              fontFamily: MONO,
              fontSize: 13,
              color: C.accent,
              letterSpacing: "0.14em",
              textTransform: "uppercase",
              borderBottom: "1px solid " + C.border,
            }}
          >
            Tasks
          </div>
          {roots.length === 0 && (
            <div style={{ padding: 20, color: C.textFaint, fontWeight: 600, fontSize: 14.5, fontFamily: MONO }}>Waiting…</div>
          )}
          {roots.map((t) => {
            const on = activeTask?.trace === t.trace;
            const sigs = subtreeSignals(t);
            const err = sigs.some((s) => s.type === "ERROR");
            const fin = sigs.some((s) => s.type === "FINAL");
            const dot = err ? colorFor("ERROR") : fin ? colorFor("FINAL") : C.textFaint;
            return (
              <div
                key={t.trace}
                onClick={() => {
                  setSelTrace(t.trace);
                  setSelSigId(null);
                }}
                title={t.hint}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                  padding: "9px 14px",
                  cursor: "pointer",
                  borderBottom: "1px solid " + C.border,
                  borderLeft: `3px solid ${on ? colorFor("TASK") : "transparent"}`,
                  background: on ? "rgba(var(--accent2-rgb), 0.08)" : "transparent",
                }}
              >
                <span style={{ width: 7, height: 7, borderRadius: "50%", background: dot, flexShrink: 0, boxShadow: `0 0 5px ${dot}` }} />
                <div style={{ minWidth: 0, flex: 1 }}>
                  <div
                    style={{
                      fontFamily: MONO,
                      fontSize: 14,
                      color: on ? C.text : C.textDim,
                      whiteSpace: "nowrap",
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                    }}
                  >
                    {t.hint || t.taskSig?.directed?.id || t.trace.slice(0, 14)}
                  </div>
                  <div style={{ fontFamily: MONO, fontSize: 12.5, color: C.textFaint, fontWeight: 600, }}>
                    {t.trace.slice(0, 12)} · {t.subtreeCount} sig{t.subtreeCount === 1 ? "" : "s"}
                  </div>
                </div>
              </div>
            );
          })}
        </div>

        {/* right: toolbar + literal node/edge tree */}
        <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column" }}>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 8,
              padding: "8px 14px",
              borderBottom: "1px solid " + C.border,
              flexShrink: 0,
            }}
          >
            <span style={{ fontFamily: MONO, fontSize: 13, color: C.textFaint, fontWeight: 600, letterSpacing: "0.1em", textTransform: "uppercase" }}>
              layout
            </span>
            <Seg active={mode === "simple"} onClick={() => setMode("simple")}>
              simple
            </Seg>
            <Seg active={mode === "rt"} onClick={() => setMode("rt")}>
              tidy · Reingold–Tilford
            </Seg>
          </div>
          <div style={{ flex: 1, overflow: "auto", minWidth: 0 }}>
            {!activeTask ? (
              <div style={{ padding: 48, textAlign: "center", color: C.textFaint, fontWeight: 600, fontFamily: MONO, fontSize: 15 }}>
                Select a task to render its signal tree.
              </div>
            ) : (
              <svg width={svgW} height={svgH} style={{ display: "block" }}>
                <g transform={`translate(${MARGIN_X}, ${MARGIN_Y})`}>
                  {edges.map((e, i) => (
                    <path
                      key={i}
                      d={`M ${e.x1} ${e.y1} C ${e.x1} ${(e.y1 + e.y2) / 2}, ${e.x2} ${(e.y1 + e.y2) / 2}, ${e.x2} ${e.y2}`}
                      fill="none"
                      stroke={e.color}
                      strokeOpacity={0.4}
                      strokeWidth={1.5}
                    />
                  ))}
                  {nodes.map((n) => {
                    const c = colorFor(n.sig.type);
                    const on = n.sig.id === selSigId;
                    return (
                      <g
                        key={n.sig.id || `${n.x}:${n.y}`}
                        transform={`translate(${n.x}, ${n.y})`}
                        onClick={() => {
                          setSelSigId(n.sig.id);
                          setFull(false);
                        }}
                        style={{ cursor: "pointer" }}
                      >
                        {on && <circle r={R + 5} fill="none" stroke={c} strokeOpacity={0.5} strokeWidth={2} />}
                        <circle r={R} fill={on ? `${c}33` : C.bgCard} stroke={c} strokeWidth={on ? 2.5 : 1.5} />
                        <text textAnchor="middle" dy="0.34em" fontFamily={MONO} fontSize={12} fontWeight={700} fill={c}>
                          {abbr(n.sig.type)}
                        </text>
                        <text textAnchor="middle" y={R + 14} fontFamily={MONO} fontSize={9.5} fill={C.textDim}>
                          {n.sig.type}
                        </text>
                      </g>
                    );
                  })}
                </g>
              </svg>
            )}
          </div>
        </div>

      {/* right: closable signal inspector rail */}
      {selSig && (
        <div
          style={{
            width: 384,
            flexShrink: 0,
            borderLeft: "1px solid " + C.border,
            background: "var(--bg-overlay)",
            display: "flex",
            flexDirection: "column",
            overflow: "hidden",
          }}
        >
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 10,
              padding: "12px 16px",
              borderBottom: "1px solid " + C.border,
              flexShrink: 0,
            }}
          >
            <span style={{ width: 10, height: 10, borderRadius: "50%", background: colorFor(selSig.type), boxShadow: `0 0 6px ${colorFor(selSig.type)}` }} />
            <span style={{ color: colorFor(selSig.type), fontFamily: MONO, fontSize: 16, fontWeight: 700 }}>{selSig.type}</span>
            <span
              onClick={() => setSelSigId(null)}
              style={{ marginLeft: "auto", color: C.textFaint, fontWeight: 600, fontSize: 19, cursor: "pointer", lineHeight: 1 }}
              title="Close"
            >
              ×
            </span>
          </div>
          <div style={{ overflowY: "auto", padding: "12px 16px" }}>
            <Field label="id" value={selSig.id} />
            <Field label="trace_id" value={selSig.trace_id} />
            <Field label="parent_id" value={selSig.parent_id ?? "—"} />
            <Field label="directed" value={selSig.directed?.id ?? "—"} />
            <Field label="ts" value={selSig.ts} />
            <div style={{ display: "flex", alignItems: "center", gap: 10, margin: "14px 0 6px" }}>
              <span style={{ fontFamily: MONO, fontSize: 13, color: C.textFaint, fontWeight: 600, letterSpacing: "0.1em", textTransform: "uppercase" }}>
                {full ? "full envelope" : "payload"}
              </span>
              <span
                onClick={() => setFull((f) => !f)}
                style={{
                  fontFamily: MONO,
                  fontSize: 13,
                  color: C.accent2,
                  cursor: "pointer",
                  border: "1px solid rgba(var(--accent2-rgb), 0.4)",
                  borderRadius: 6,
                  padding: "2px 9px",
                }}
              >
                {full ? "show payload" : "expand full signal"}
              </span>
            </div>
            <pre style={preStyle}>
              {JSON.stringify(full ? selSig : selSig.payload ?? {}, null, 2)}
            </pre>
          </div>
        </div>
      )}
    </div>
  );
}

function Seg({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <span
      onClick={onClick}
      style={{
        fontFamily: MONO,
        fontSize: 13,
        cursor: "pointer",
        userSelect: "none",
        color: active ? C.accent2 : C.textDim,
        background: active ? "rgba(var(--accent2-rgb), 0.12)" : "transparent",
        border: "1px solid " + (active ? "rgba(var(--accent2-rgb), 0.4)" : C.border),
        borderRadius: 6,
        padding: "3px 10px",
        whiteSpace: "nowrap",
      }}
    >
      {children}
    </span>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div style={{ display: "flex", gap: 10, padding: "2px 0", alignItems: "baseline" }}>
      <span style={{ fontFamily: MONO, fontSize: 13, color: C.textFaint, fontWeight: 600, width: 74, flexShrink: 0 }}>{label}</span>
      <span style={{ fontFamily: MONO, fontSize: 13.5, color: C.textDim, fontWeight: 600, wordBreak: "break-all", flex: 1, minWidth: 0 }}>{value}</span>
    </div>
  );
}

const preStyle: React.CSSProperties = {
  margin: 0,
  padding: 10,
  background: "var(--bg-well)",
  border: "1px solid var(--border)",
  borderRadius: 8,
  color: "var(--text-dim)",
  fontSize: 13,
  fontFamily: MONO,
  whiteSpace: "pre-wrap",
  wordBreak: "break-all",
};
