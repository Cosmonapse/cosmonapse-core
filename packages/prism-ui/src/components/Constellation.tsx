import { useEffect, useMemo, useState } from "react";
import {
  computeConsistency,
  consistencyColor,
  type EdgeChannel,
  type GraphEdge,
  type GraphNode,
  type RunGraph,
  type SetupGroup,
} from "../constellation";
import { fmtMs, fmtPct } from "../metrics";
import { C, MONO } from "../theme";
import type { Signal } from "../types";

// Kind identity colors — kept in sync with PrismCanvas / theme TYPE_COLOR.
// Read through a call, not a frozen object: these feed SVG presentation
// attributes, which cannot resolve var(), so they must re-read the live
// palette on every render.
const kindColor = (): Record<GraphNode["kind"], string> => ({
  neuron: C.accent2,
  engram: C.engram,
  effector: C.effector,
  receptor: C.receptor,
});

const channelColor = (): Record<EdgeChannel, string> => ({
  task: C.synapse,
  tool: C.effector,
  recall: C.engram,
  imprint: C.imprint,
  output: C.ok,
});

const PANEL_WIDTH = 320;

interface Props {
  signals: Signal[];
}

/**
 * Constellation — one task run drawn as its execution graph. Nodes are the
 * neurons, engrams and effectors that took part; edges are the typed
 * interactions between them (drawn sender → receiver; the synapse is
 * transport and never appears as a node). When a setup has run more than once, edges that
 * did not appear in every run render dashed, and the setup carries a
 * structural-consistency score (mean pairwise Jaccard over run signatures).
 */
export function Constellation({ signals }: Props) {
  const report = useMemo(() => computeConsistency(signals), [signals]);
  const [selected, setSelected] = useState<string | null>(null);

  // Keep the selection valid as the rolling buffer evicts old traces.
  const run = (selected && report.runsByTrace.get(selected)) || report.setups[0]?.runs[0] || null;
  useEffect(() => {
    if (selected && !report.runsByTrace.has(selected)) setSelected(null);
  }, [selected, report]);

  const setup = run ? report.setups.find((g) => g.key === run.setupKey) ?? null : null;

  return (
    <div style={{ position: "absolute", top: 64, left: 0, right: 0, bottom: 0, display: "flex", background: "var(--bg-view)" }}>
      {/* ── setup / run list ── */}
      <div style={{ width: PANEL_WIDTH, flexShrink: 0, borderRight: "1px solid " + C.border, background: "var(--bg-rail)", display: "flex", flexDirection: "column" }}>
        <div style={{ padding: "12px 14px", fontFamily: MONO, fontSize: 13, color: C.accent, letterSpacing: "0.14em", textTransform: "uppercase", borderBottom: "1px solid " + C.border, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <span>Setups · Runs</span>
          <span
            title="Structural consistency across all setups that ran more than once — pair-weighted mean Jaccard similarity of run graphs."
            style={{ color: consistencyColor(report.overall), letterSpacing: 0, textTransform: "none", cursor: "help" }}
          >
            {report.overall == null ? "— consistency" : fmtPct(report.overall) + " consistent"}
          </span>
        </div>
        <div style={{ overflowY: "auto", flex: 1 }}>
          {report.setups.length === 0 && (
            <div style={{ padding: 24, textAlign: "center", color: C.textFaint, fontWeight: 600, fontSize: 14.5, fontFamily: MONO }}>
              Waiting for tasks…
            </div>
          )}
          {report.setups.map((g) => (
            <SetupRow key={g.key} group={g} selected={run?.trace ?? null} onSelect={setSelected} />
          ))}
        </div>
      </div>

      {/* ── graph ── */}
      <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column" }}>
        {run ? (
          <>
            <RunHeader run={run} setup={setup} />
            <div style={{ flex: 1, overflow: "auto" }}>
              <GraphSvg run={run} comparable={(setup?.runs.length ?? 0) > 1} />
            </div>
            <Legend comparable={(setup?.runs.length ?? 0) > 1} />
          </>
        ) : (
          <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", color: C.textFaint, fontWeight: 600, fontFamily: MONO, fontSize: 15 }}>
            Dispatch a task to see its constellation.
          </div>
        )}
      </div>
    </div>
  );
}

// ── left panel rows ─────────────────────────────────────────────────────────
function SetupRow({ group, selected, onSelect }: { group: SetupGroup; selected: string | null; onSelect: (t: string) => void }) {
  const cc = consistencyColor(group.consistency);
  return (
    <div style={{ borderBottom: "1px solid " + C.border }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "9px 14px 5px" }}>
        <span title={group.label} style={{ flex: 1, minWidth: 0, fontFamily: MONO, fontSize: 14, color: C.text, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
          {group.label}
        </span>
        <span
          title={
            group.consistency == null
              ? "Single run — consistency needs at least two runs of the same setup."
              : `Mean pairwise Jaccard similarity of the ${group.runs.length} run graphs.`
          }
          style={{ flexShrink: 0, fontFamily: MONO, fontSize: 13, color: cc, background: cc + "14", border: `1px solid ${cc}40`, borderRadius: 6, padding: "1px 7px", cursor: "help" }}
        >
          {group.consistency == null ? `${group.runs.length} run` : fmtPct(group.consistency)}
        </span>
      </div>
      {group.runs.map((r) => {
        const on = r.trace === selected;
        const statusColor = r.error ? C.danger : r.final ? C.ok : C.textFaint;
        return (
          <div
            key={r.trace}
            onClick={() => onSelect(r.trace)}
            style={{
              display: "flex",
              alignItems: "center",
              gap: 8,
              padding: "6px 14px 6px 22px",
              cursor: "pointer",
              background: on ? "rgba(var(--accent2-rgb), 0.08)" : "transparent",
              borderLeft: `3px solid ${on ? C.accent2 : "transparent"}`,
            }}
          >
            <span style={{ width: 6, height: 6, borderRadius: "50%", background: statusColor, boxShadow: `0 0 4px ${statusColor}`, flexShrink: 0 }} />
            <span style={{ fontFamily: MONO, fontSize: 13, color: on ? C.accent2 : C.textDim }}>
              {new Date(r.start).toISOString().slice(11, 19)}
            </span>
            <span style={{ fontFamily: MONO, fontSize: 13, color: C.textFaint, fontWeight: 600, marginLeft: "auto" }}>
              {r.nodes.length}n · {r.edges.length}e · {fmtMs(r.end - r.start)}
            </span>
          </div>
        );
      })}
    </div>
  );
}

function RunHeader({ run, setup }: { run: RunGraph; setup: SetupGroup | null }) {
  const status = run.error ? "error" : run.final ? "final" : "open";
  const statusColor = run.error ? C.danger : run.final ? C.ok : C.textFaint;
  const siblings = setup ? setup.runs.length : 1;
  return (
    <div style={{ flexShrink: 0, display: "flex", alignItems: "center", gap: 12, padding: "9px 18px", borderBottom: "1px solid " + C.border }}>
      <span style={{ width: 7, height: 7, borderRadius: "50%", background: statusColor, boxShadow: `0 0 5px ${statusColor}`, flexShrink: 0 }} />
      <span title={run.label} style={{ minWidth: 0, fontFamily: MONO, fontSize: 14.5, color: C.text, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
        {run.label}
      </span>
      <span style={{ flexShrink: 0, fontFamily: MONO, fontSize: 13, color: C.textFaint, fontWeight: 600, }}>
        {status} · {fmtMs(run.end - run.start)}
      </span>
      <span style={{ marginLeft: "auto", flexShrink: 0, fontFamily: MONO, fontSize: 13, color: setup?.consistency == null ? C.textFaint : consistencyColor(setup.consistency) }}>
        {setup?.consistency == null
          ? siblings > 1
            ? ""
            : "single run — no comparison yet"
          : `graph consistency ${fmtPct(setup.consistency)} across ${siblings} runs`}
      </span>
    </div>
  );
}

// ── the graph itself ────────────────────────────────────────────────────────
const COL_W = 230;
const ROW_H = 96;
const PAD_X = 90;
const PAD_Y = 70;
const R = 21;

interface Placed extends GraphNode {
  x: number;
  y: number;
}

/** Layered layout: BFS depth from the root senders (nodes nothing points at)
 *  along edge direction gives the column; nodes in a column spread vertically,
 *  centered on the canvas. */
function layout(run: RunGraph): { placed: Map<string, Placed>; width: number; height: number } {
  const out = new Map<string, string[]>();
  const hasIncoming = new Set<string>();
  for (const e of run.edges) {
    if (e.channel === "output") continue; // FINAL flows back — no new column
    if (e.from === e.to) continue; // self-delegation loop — no new column
    const a = out.get(e.from) ?? [];
    a.push(e.to);
    out.set(e.from, a);
    hasIncoming.add(e.to);
  }
  const depth = new Map<string, number>();
  const queue: string[] = [];
  for (const n of run.nodes)
    if (!hasIncoming.has(n.id)) {
      depth.set(n.id, 0);
      queue.push(n.id);
    }
  while (queue.length) {
    const cur = queue.shift()!;
    for (const next of out.get(cur) ?? [])
      if (!depth.has(next)) {
        depth.set(next, depth.get(cur)! + 1);
        queue.push(next);
      }
  }
  for (const n of run.nodes) if (!depth.has(n.id)) depth.set(n.id, 1); // cycle remnant — park with the workers

  const cols = new Map<number, GraphNode[]>();
  for (const n of run.nodes) {
    const d = depth.get(n.id)!;
    const a = cols.get(d) ?? [];
    a.push(n);
    cols.set(d, a);
  }
  // stable vertical order: receptors, then neurons, engrams, effectors, then id
  // Receptors lead: they are where a run enters the system.
  const kindOrder: Record<GraphNode["kind"], number> = { receptor: 0, neuron: 1, engram: 2, effector: 3 };
  for (const a of cols.values()) a.sort((x, y) => kindOrder[x.kind] - kindOrder[y.kind] || x.id.localeCompare(y.id));

  const maxDepth = cols.size ? Math.max(...cols.keys()) : 0;
  const maxRows = Math.max(...[...cols.values()].map((a) => a.length));
  const width = PAD_X * 2 + maxDepth * COL_W + 2 * R;
  const height = Math.max(360, PAD_Y * 2 + (maxRows - 1) * ROW_H + 2 * R);

  const placed = new Map<string, Placed>();
  for (const [d, a] of cols) {
    const x = PAD_X + R + d * COL_W;
    const colH = (a.length - 1) * ROW_H;
    const y0 = height / 2 - colH / 2;
    a.forEach((n, i) => placed.set(n.id, { ...n, x, y: y0 + i * ROW_H }));
  }
  return { placed, width, height };
}

function GraphSvg({ run, comparable }: { run: RunGraph; comparable: boolean }) {
  const { placed, width, height } = useMemo(() => layout(run), [run]);

  // group parallel edges between the same node pair to fan out their curves
  const byPair = new Map<string, GraphEdge[]>();
  for (const e of run.edges) {
    const key = e.from < e.to ? `${e.from}|${e.to}` : `${e.to}|${e.from}`;
    const a = byPair.get(key) ?? [];
    a.push(e);
    byPair.set(key, a);
  }

  return (
    <svg width={width} height={height} style={{ display: "block", margin: "0 auto", minWidth: "60%" }}>
      <defs>
        {Object.entries(channelColor()).map(([ch, color]) => (
          <marker key={ch} id={`arrow-${ch}`} viewBox="0 0 8 8" refX={7} refY={4} markerWidth={7} markerHeight={7} orient="auto-start-reverse">
            <path d="M0,0 L8,4 L0,8 z" fill={color} />
          </marker>
        ))}
      </defs>

      {[...byPair.values()].map((group) =>
        group.map((e, i) => {
          const a = placed.get(e.from);
          const b = placed.get(e.to);
          if (!a || !b) return null;
          const color = channelColor()[e.channel];
          const dashed = comparable && e.freq < 1;
          const key = `${e.from}|${e.to}|${e.channel}`;
          const title = `${e.from} → ${e.to} · ${e.channel} · ${e.count} request${e.count === 1 ? "" : "s"}${e.replies ? `, ${e.replies} result${e.replies === 1 ? "" : "s"}` : ""}${comparable ? ` · in ${Math.round(e.freq * 100)}% of runs` : ""}`;

          // self-delegation: a task the neuron dispatched to itself — loop arc
          if (e.from === e.to) {
            return (
              <g key={key}>
                <path
                  d={`M ${a.x - 11} ${a.y - R + 3} C ${a.x - 48} ${a.y - R - 46}, ${a.x + 48} ${a.y - R - 46}, ${a.x + 11} ${a.y - R + 3}`}
                  fill="none"
                  stroke={color}
                  strokeWidth={1.6}
                  strokeDasharray={dashed ? "5 5" : undefined}
                  opacity={dashed ? 0.55 : 0.85}
                  markerEnd={`url(#arrow-${e.channel})`}
                >
                  <title>{title}</title>
                </path>
                <text x={a.x} y={a.y - R - 40} textAnchor="middle" style={{ fontFamily: MONO, fontSize: 12.5, fill: color, opacity: 0.9 }}>
                  {e.count > 1 ? `${e.count}×` : ""}
                </text>
              </g>
            );
          }

          // fan parallel edges apart; arc backward edges (e.g. FINAL) wider
          const fan = (i - (group.length - 1) / 2) * 34;
          const back = b.x < a.x ? 70 : 0;
          const mx = (a.x + b.x) / 2;
          const my = (a.y + b.y) / 2;
          const dx = b.x - a.x;
          const dy = b.y - a.y;
          const len = Math.hypot(dx, dy) || 1;
          const nx = -dy / len;
          const ny = dx / len;
          const cx = mx + nx * (fan + back);
          const cy = my + ny * (fan + back);
          // trim the path at the node radii
          const t = (R + 4) / len;
          const x1 = a.x + dx * t;
          const y1 = a.y + dy * t;
          const x2 = b.x - dx * t;
          const y2 = b.y - dy * t;
          // return curve (replies) mirrored on the other side of the chord
          const rx = mx - nx * (fan + back + 14);
          const ry = my - ny * (fan + back + 14);
          return (
            <g key={key}>
              <path
                d={`M ${x1} ${y1} Q ${cx} ${cy} ${x2} ${y2}`}
                fill="none"
                stroke={color}
                strokeWidth={1.6}
                strokeDasharray={dashed ? "5 5" : undefined}
                opacity={dashed ? 0.55 : 0.85}
                markerEnd={`url(#arrow-${e.channel})`}
              >
                <title>{title}</title>
              </path>
              <text
                x={(x1 + 2 * cx + x2) / 4}
                y={(y1 + 2 * cy + y2) / 4 - 5}
                textAnchor="middle"
                style={{ fontFamily: MONO, fontSize: 12.5, fill: color, opacity: 0.9 }}
              >
                {e.count > 1 ? `${e.count}×` : ""}
              </text>
              {e.replies > 0 && (
                <>
                  <path
                    d={`M ${x2} ${y2} Q ${rx} ${ry} ${x1} ${y1}`}
                    fill="none"
                    stroke={color}
                    strokeWidth={1}
                    opacity={0.5}
                    markerEnd={`url(#arrow-${e.channel})`}
                  >
                    <title>{title}</title>
                  </path>
                  <text
                    x={(x2 + 2 * rx + x1) / 4}
                    y={(y2 + 2 * ry + y1) / 4 + 11}
                    textAnchor="middle"
                    style={{ fontFamily: MONO, fontSize: 12, fill: color, opacity: 0.6 }}
                  >
                    {e.replies}↩
                  </text>
                </>
              )}
            </g>
          );
        }),
      )}

      {[...placed.values()].map((n) => {
        const color = kindColor()[n.kind];
        const short = n.id.length > 22 ? n.id.slice(0, 21) + "…" : n.id;
        return (
          <g key={n.id}>
            <title>{`${n.id} · ${n.kind} · ${n.activity} signal${n.activity === 1 ? "" : "s"}${n.errors ? ` · ${n.errors} errors` : ""}`}</title>
            <circle cx={n.x} cy={n.y} r={R + 6} fill={color} opacity={0.09} />
            <circle
              cx={n.x}
              cy={n.y}
              r={R}
              fill={C.bgCard}
              stroke={n.errors ? C.danger : color}
              strokeWidth={1.6}
            />
            <circle cx={n.x} cy={n.y} r={5.5} fill={color} />
            <text x={n.x} y={n.y + R + 16} textAnchor="middle" style={{ fontFamily: MONO, fontSize: 13, fill: C.text }}>
              {short}
            </text>
            <text x={n.x} y={n.y + R + 29} textAnchor="middle" style={{ fontFamily: MONO, fontSize: 11.5, fill: color, letterSpacing: "0.08em" }}>
              {n.kind.toUpperCase()}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

function Legend({ comparable }: { comparable: boolean }) {
  const item = (color: string, label: string, dashed?: boolean) => (
    <span key={label} style={{ display: "flex", alignItems: "center", gap: 5, fontFamily: MONO, fontSize: 13, color: C.textFaint, fontWeight: 600, }}>
      {dashed ? (
        <svg width={16} height={6}><line x1={0} y1={3} x2={16} y2={3} stroke={color} strokeWidth={1.6} strokeDasharray="4 3" /></svg>
      ) : (
        <span style={{ width: 9, height: 9, borderRadius: 2, background: color }} />
      )}
      {label}
    </span>
  );
  return (
    <div style={{ flexShrink: 0, display: "flex", gap: 16, flexWrap: "wrap", padding: "9px 18px", borderTop: "1px solid " + C.border }}>
      {item(kindColor().receptor, "receptor")}
      {item(kindColor().neuron, "neuron")}
      {item(kindColor().engram, "engram")}
      {item(kindColor().effector, "effector")}
      <span style={{ color: C.textFaint, fontWeight: 600, }}>│</span>
      {item(channelColor().task, "task")}
      {item(channelColor().tool, "tool")}
      {item(channelColor().recall, "recall")}
      {item(channelColor().imprint, "imprint")}
      {item(channelColor().output, "final")}
      <span key="reply" style={{ display: "flex", alignItems: "center", gap: 5, fontFamily: MONO, fontSize: 13, color: C.textFaint, fontWeight: 600, }}>
        <svg width={16} height={6}><line x1={16} y1={3} x2={2} y2={3} stroke={C.textDim} strokeWidth={1} /><path d="M6,0 L0,3 L6,6 z" fill={C.textDim} /></svg>
        result / reply
      </span>
      {comparable && (
        <>
          <span style={{ color: C.textFaint, fontWeight: 600, }}>│</span>
          {item(C.textDim, "not in every run", true)}
        </>
      )}
    </div>
  );
}
