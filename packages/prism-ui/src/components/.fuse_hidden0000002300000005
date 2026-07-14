import { useMemo, useState } from "react";
import { C, MONO, colorFor } from "../theme";
import type { Signal, SignalType } from "../types";

interface Props {
  open: boolean;
  width: number;
  signals: Signal[];
  selected: Signal | null;
  onSelect: (sig: Signal | null) => void;
}

// Traces made up purely of these types are housekeeping, not tasks — they get
// folded into one "lifecycle" bucket so grouped view stays readable.
const LIFECYCLE = new Set<SignalType>(["REGISTER", "DEREGISTER", "HEARTBEAT", "DISCOVER"]);

// Signal types that OPEN a request pathway. Their replies carry
// parent_id = opener.id, so opener + replies group together.
const REQUEST_TYPES = new Set<SignalType>([
  "RECALL",
  "IMPRINT",
  "CLARIFICATION",
  "PERMISSION",
  "TASK_OFFER",
]);

// Engram / memory traffic. A trace made ONLY of these (no TASK) is an orphan
// side-effect — e.g. an imprint dispatched from a detector hook gets a fresh
// trace_id — and is stitched into the task that was in flight at that moment.
const ENGRAM_TYPES = new Set<SignalType>([
  "RECALL",
  "RECALLED",
  "IMPRINT",
  "IMPRINTED",
  "MEMORY_APPEND",
  "CONTEXT_SYNC",
]);

// Max age gap (ms) allowed when stitching an orphan engram trace to a task.
const STITCH_WINDOW_MS = 60_000;

const MAIN_PATHWAY = "__main__";

interface PathwayGroup {
  key: string;
  label: string;
  /** True when the group was linked by time, not by trace/parent ids. */
  approx: boolean;
  signals: Signal[];
}

interface TaskGroup {
  trace: string;
  hint?: string;
  count: number;
  pathways: PathwayGroup[];
}

interface Grouped {
  tasks: TaskGroup[];
  lifecycle: Signal[];
}

/** Pull a human hint for a task group out of its TASK signal payload. */
function taskHint(sig: Signal | undefined): string | undefined {
  if (!sig?.payload) return undefined;
  for (const k of ["input", "prompt", "task", "description", "goal"]) {
    const v = sig.payload[k];
    if (typeof v === "string" && v.trim()) return v.trim();
    if (v && typeof v === "object") {
      // common shape: payload.input.prompt
      for (const kk of ["prompt", "task", "text", "query"]) {
        const vv = (v as Record<string, unknown>)[kk];
        if (typeof vv === "string" && vv.trim()) return vv.trim();
      }
    }
  }
  return undefined;
}

const ts = (s: Signal) => new Date(s.ts).getTime() || 0;

/**
 * Group the rolling buffer into:  task (trace) → pathway → signals.
 *
 * A pathway is the SDK's request/reply unit: signals with no request lineage
 * sit on the main task pathway; a request signal (RECALL, IMPRINT,
 * CLARIFICATION, …) opens a sub-pathway that collects its replies
 * (parent_id = request id). Within a task, signals read chronologically —
 * the same shape as the execution trace.
 *
 * Orphan engram traces (imprints fired outside the task context get a fresh
 * trace_id) are stitched into the task whose signals immediately precede
 * them in the stream, and marked approximate.
 */
function groupSignals(signals: Signal[]): Grouped {
  // signals arrive newest-first; iterate in that order so task groups are
  // ordered by most recent activity.
  const byTrace = new Map<string, Signal[]>();
  const byId = new Map<string, Signal>();
  for (const sig of signals) {
    const trace = sig.trace_id || "no-trace";
    let arr = byTrace.get(trace);
    if (!arr) byTrace.set(trace, (arr = []));
    arr.push(sig);
    if (sig.id) byId.set(sig.id, sig);
  }

  const lifecycle: Signal[] = [];
  const taskTraces = new Map<string, Signal[]>(); // insertion = recency order
  const orphans: { trace: string; sigs: Signal[] }[] = [];

  for (const [trace, sigs] of byTrace) {
    if (sigs.every((s) => LIFECYCLE.has(s.type))) {
      lifecycle.push(...sigs);
    } else if (!sigs.some((s) => s.type === "TASK") && sigs.every((s) => ENGRAM_TYPES.has(s.type))) {
      orphans.push({ trace, sigs });
    } else {
      taskTraces.set(trace, sigs);
    }
  }

  // ── stitch orphan engram traces into the task in flight at that time ──
  const stitched = new Set<string>(); // signal ids that were stitched in
  for (const o of orphans) {
    const oldest = o.sigs[o.sigs.length - 1];
    const start = signals.indexOf(oldest);
    let owner: string | null = null;
    // Walk older signals in stream order; the first one that belongs to a
    // task trace is the task that was running when the orphan began.
    for (let i = start + 1; i < signals.length; i++) {
      const cand = signals[i];
      const candTrace = cand.trace_id || "no-trace";
      if (!taskTraces.has(candTrace)) continue;
      if (ts(oldest) - ts(cand) > STITCH_WINDOW_MS) break;
      owner = candTrace;
      break;
    }
    if (owner) {
      const merged = [...taskTraces.get(owner)!, ...o.sigs];
      merged.sort((a, b) => ts(b) - ts(a)); // keep newest-first invariant
      taskTraces.set(owner, merged);
      for (const s of o.sigs) stitched.add(s.id);
    } else {
      taskTraces.set(o.trace, o.sigs); // no candidate — keep as its own group
    }
  }

  // ── build pathway groups inside each task ──
  const tasks: TaskGroup[] = [];
  for (const [trace, sigs] of taskTraces) {
    const pathways = new Map<string, Signal[]>();
    for (const sig of sigs) {
      let key = MAIN_PATHWAY;
      if (REQUEST_TYPES.has(sig.type)) {
        key = sig.id || MAIN_PATHWAY; // request opens its own pathway
      } else if (sig.parent_id) {
        const parent = byId.get(sig.parent_id);
        // Reply to a request → that request's pathway. Parent unseen →
        // its own thread. Parent is the TASK itself → main pathway.
        if (!parent || REQUEST_TYPES.has(parent.type)) key = sig.parent_id;
      }
      let arr = pathways.get(key);
      if (!arr) pathways.set(key, (arr = []));
      arr.push(sig);
    }

    const groups: PathwayGroup[] = [];
    for (const [key, arr] of pathways) {
      arr.sort((a, b) => ts(a) - ts(b)); // chronological within a pathway
      if (key === MAIN_PATHWAY) {
        groups.push({ key, label: "task pathway", approx: false, signals: arr });
        continue;
      }
      const opener = byId.get(key);
      const approx = arr.some((s) => stitched.has(s.id));
      const label = opener
        ? `↳ ${opener.type}${opener.directed?.id ? " · " + opener.directed.id : ""}`
        : `↳ thread ${key.slice(4, 12)}`;
      groups.push({ key, label, approx, signals: arr });
    }
    // main first, then sub-pathways in the order they opened
    groups.sort((a, b) => {
      if (a.key === MAIN_PATHWAY) return -1;
      if (b.key === MAIN_PATHWAY) return 1;
      return ts(a.signals[0]) - ts(b.signals[0]);
    });

    const taskSig = [...sigs].reverse().find((s) => s.type === "TASK");
    tasks.push({ trace, hint: taskHint(taskSig), count: sigs.length, pathways: groups });
  }

  return { tasks, lifecycle };
}

export function Sidebar({ open, width, signals, selected, onSelect }: Props) {
  const [grouped, setGrouped] = useState(false);
  // Signal ids showing the full envelope (not just the payload).
  const [expandedFull, setExpandedFull] = useState<Set<string>>(() => new Set());
  // Collapsed group keys: "t:<trace>" and "p:<trace>:<pathway>".
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());

  const groups = useMemo(() => (grouped ? groupSignals(signals) : null), [grouped, signals]);

  const rowKey = (sig: Signal, i: number) => sig.id || `${sig.trace_id}:${sig.ts}:${i}`;

  const toggleFull = (key: string) =>
    setExpandedFull((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

  const toggleCollapsed = (key: string) =>
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

  const renderRow = (sig: Signal, i: number, indent = 0) => {
    const key = rowKey(sig, i);
    return (
      <SignalRow
        key={key}
        sig={sig}
        indent={indent}
        isSel={selected === sig}
        isFull={expandedFull.has(key)}
        onClick={() => onSelect(selected === sig ? null : sig)}
        onToggleFull={() => toggleFull(key)}
      />
    );
  };

  return (
    <aside
      style={{
        position: "absolute",
        top: 64,
        right: 0,
        bottom: 0,
        width: open ? width : 0,
        background: "rgba(7,8,12,0.85)",
        WebkitBackdropFilter: "blur(20px)",
        backdropFilter: "blur(20px)",
        borderLeft: open ? "1px solid " + C.border : "none",
        transition: "width 0.25s ease",
        overflow: "hidden",
        zIndex: 4,
        display: "flex",
        flexDirection: "column",
      }}
    >
      <div
        style={{
          flexShrink: 0,
          padding: "14px 16px",
          borderBottom: "1px solid " + C.border,
          display: "flex",
          alignItems: "center",
          gap: 10,
        }}
      >
        <span
          style={{
            fontFamily: MONO,
            fontSize: 11,
            color: C.accent,
            letterSpacing: "0.14em",
            textTransform: "uppercase",
          }}
        >
          Signal stream
        </span>
        <span
          onClick={() => setGrouped((g) => !g)}
          title="Group signals by task, then by pathway"
          style={{
            fontFamily: MONO,
            fontSize: 10.5,
            color: grouped ? C.accent2 : C.textFaint,
            cursor: "pointer",
            userSelect: "none",
            border: "1px solid " + (grouped ? "rgba(34,211,238,0.4)" : C.borderStrong),
            borderRadius: 6,
            padding: "2px 8px",
            transition: "all 0.15s",
            whiteSpace: "nowrap",
          }}
        >
          {grouped ? "group: task ▸ pathway" : "group: off"}
        </span>
        <span style={{ marginLeft: "auto", color: C.textFaint, fontSize: 12, fontFamily: MONO }}>
          {signals.length}
        </span>
      </div>

      <div style={{ flex: 1, overflowY: "auto" }}>
        {signals.length === 0 && (
          <div style={{ padding: 48, textAlign: "center", color: C.textFaint, fontSize: 13 }}>
            Waiting for signals…
          </div>
        )}

        {!groups && signals.map((sig, i) => renderRow(sig, i))}

        {groups && (
          <>
            {groups.tasks.map((task) => {
              const tKey = `t:${task.trace}`;
              const tCollapsed = collapsed.has(tKey);
              return (
                <div key={tKey}>
                  <GroupHeader
                    level={0}
                    collapsed={tCollapsed}
                    color={colorFor("TASK")}
                    tag="task"
                    title={task.trace.slice(0, 16)}
                    hint={task.hint}
                    count={task.count}
                    onClick={() => toggleCollapsed(tKey)}
                  />
                  {!tCollapsed &&
                    task.pathways.map((pw) => {
                      const pKey = `p:${task.trace}:${pw.key}`;
                      const pCollapsed = collapsed.has(pKey);
                      return (
                        <div key={pKey}>
                          <GroupHeader
                            level={1}
                            collapsed={pCollapsed}
                            color={C.accent}
                            title={pw.label}
                            approx={pw.approx}
                            count={pw.signals.length}
                            onClick={() => toggleCollapsed(pKey)}
                          />
                          {!pCollapsed && pw.signals.map((sig, i) => renderRow(sig, i, 26))}
                        </div>
                      );
                    })}
                </div>
              );
            })}

            {groups.lifecycle.length > 0 && (
              <div>
                <GroupHeader
                  level={0}
                  collapsed={collapsed.has("t:__lifecycle__")}
                  color={colorFor("HEARTBEAT")}
                  tag="lifecycle"
                  title="register · heartbeat · discover"
                  count={groups.lifecycle.length}
                  onClick={() => toggleCollapsed("t:__lifecycle__")}
                />
                {!collapsed.has("t:__lifecycle__") &&
                  groups.lifecycle.map((sig, i) => renderRow(sig, i, 14))}
              </div>
            )}
          </>
        )}
      </div>
    </aside>
  );
}

// ── group header row ──────────────────────────────────────────────────────
function GroupHeader({ level, collapsed, color, tag, title, hint, count, approx, onClick }: {
  level: 0 | 1;
  collapsed: boolean;
  color: string;
  tag?: string;
  title: string;
  hint?: string;
  count: number;
  approx?: boolean;
  onClick: () => void;
}) {
  return (
    <div
      onClick={onClick}
      title={approx ? "Linked by time — this engram op was dispatched on a separate trace" : undefined}
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        padding: level === 0 ? "9px 16px" : "6px 16px 6px 28px",
        cursor: "pointer",
        userSelect: "none",
        background: level === 0 ? "rgba(255,255,255,0.025)" : "transparent",
        borderBottom: "1px solid " + C.border,
      }}
    >
      <span style={{ color: C.textFaint, fontSize: 9, width: 10, flexShrink: 0 }}>
        {collapsed ? "▶" : "▼"}
      </span>
      {tag && (
        <span
          style={{
            fontFamily: MONO,
            fontSize: 9.5,
            color,
            border: `1px solid ${color}40`,
            background: `${color}12`,
            borderRadius: 5,
            padding: "1px 6px",
            letterSpacing: "0.08em",
            textTransform: "uppercase",
            flexShrink: 0,
          }}
        >
          {tag}
        </span>
      )}
      <span
        style={{
          fontFamily: MONO,
          fontSize: level === 0 ? 11.5 : 10.5,
          color: level === 0 ? C.textDim : color,
          whiteSpace: "nowrap",
          overflow: "hidden",
          textOverflow: "ellipsis",
          flexShrink: hint ? 0 : 1,
        }}
      >
        {title}
        {approx && <span style={{ color: C.textFaint }}> ≈</span>}
      </span>
      {hint && (
        <span
          style={{
            fontFamily: MONO,
            fontSize: 10,
            color: C.textFaint,
            whiteSpace: "nowrap",
            overflow: "hidden",
            textOverflow: "ellipsis",
            minWidth: 0,
          }}
        >
          {hint}
        </span>
      )}
      <span
        style={{
          marginLeft: "auto",
          color: C.textFaint,
          fontSize: 10,
          fontFamily: MONO,
          flexShrink: 0,
        }}
      >
        {count}
      </span>
    </div>
  );
}

// ── one signal row ────────────────────────────────────────────────────────
function SignalRow({ sig, indent, isSel, isFull, onClick, onToggleFull }: {
  sig: Signal;
  indent: number;
  isSel: boolean;
  isFull: boolean;
  onClick: () => void;
  onToggleFull: () => void;
}) {
  const c = colorFor(sig.type);
  const time = safeTime(sig.ts);
  return (
    <div
      onClick={onClick}
      style={{
        padding: `10px 16px 10px ${16 + indent}px`,
        cursor: "pointer",
        borderBottom: "1px solid " + C.border,
        background: isSel ? "rgba(139,92,246,0.08)" : "transparent",
        transition: "background 0.15s",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
        <span
          style={{
            display: "inline-block",
            width: 8,
            height: 8,
            borderRadius: "50%",
            background: c,
            boxShadow: `0 0 6px ${c}`,
            flexShrink: 0,
          }}
        />
        <span
          style={{
            color: c,
            fontFamily: MONO,
            fontSize: 11.5,
            fontWeight: 600,
            letterSpacing: "0.03em",
          }}
        >
          {sig.type}
        </span>
        <span
          style={{
            marginLeft: "auto",
            color: C.textFaint,
            fontSize: 10.5,
            fontFamily: MONO,
          }}
        >
          {time}
        </span>
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <span
          style={{
            color: C.textDim,
            fontSize: 11.5,
            fontFamily: MONO,
            whiteSpace: "nowrap",
            overflow: "hidden",
            textOverflow: "ellipsis",
            flex: 1,
            minWidth: 0,
          }}
        >
          {sig.directed?.id || " - "}
          <span style={{ color: C.textFaint }}> · {(sig.trace_id || "").slice(4, 12)}</span>
        </span>
        <span
          onClick={(e) => {
            e.stopPropagation();
            onToggleFull();
          }}
          style={{
            color: isFull ? C.accent2 : C.textFaint,
            fontSize: 10,
            fontFamily: MONO,
            cursor: "pointer",
            userSelect: "none",
            flexShrink: 0,
            textDecoration: "underline",
            textDecorationColor: "rgba(255,255,255,0.2)",
            textUnderlineOffset: 2,
          }}
        >
          {isFull ? "show less" : "expand more"}
        </span>
      </div>
      {isFull && <pre style={preStyle}>{JSON.stringify(sig, null, 2)}</pre>}
      {!isFull && isSel && sig.payload && (
        <pre style={preStyle}>{JSON.stringify(sig.payload, null, 2)}</pre>
      )}
    </div>
  );
}

const preStyle: React.CSSProperties = {
  marginTop: 8,
  padding: 8,
  background: "rgba(0,0,0,0.3)",
  borderRadius: 6,
  color: C.textDim,
  fontSize: 10.5,
  fontFamily: MONO,
  whiteSpace: "pre-wrap",
  wordBreak: "break-all",
  maxHeight: 240,
  overflowY: "auto",
};

function safeTime(ts: string): string {
  const d = new Date(ts);
  return isNaN(d.getTime()) ? ts : d.toISOString().slice(11, 23);
}
