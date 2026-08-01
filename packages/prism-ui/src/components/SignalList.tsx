import { useMemo, useState } from "react";
import { C, MONO, colorFor } from "../theme";
import { groupSignals, ts, type TaskGroup } from "../grouping";
import { fmtMs } from "../metrics";
import type { Signal } from "../types";

interface Props {
  signals: Signal[];
}

interface SigNode {
  sig: Signal;
  children: SigNode[];
}

/** Signals on this task's OWN trace(s), chronological, excluding child tasks. */
function ownSignals(task: TaskGroup): Signal[] {
  const out: Signal[] = [];
  for (const pw of task.pathways) out.push(...pw.signals);
  return out.sort((a, b) => ts(a) - ts(b));
}

/** Build the parent_id tree for a task's own signals; the TASK node itself is
 *  peeled off (it's already the task header) and its children float to the top. */
function ownSignalForest(task: TaskGroup): SigNode[] {
  const sigs = ownSignals(task);
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
  const forest = roots.sort((a, b) => ts(a) - ts(b)).map(build);
  // peel the TASK node so its lineage floats up under the task header
  return forest.flatMap((n) => (n.sig.type === "TASK" ? n.children : [n]));
}

function taskDuration(task: TaskGroup): number {
  const sigs = ownSignals(task);
  if (sigs.length === 0) return 0;
  return Math.max(0, ts(sigs[sigs.length - 1]) - ts(sigs[0]));
}

function taskStatus(task: TaskGroup): string {
  const sigs = ownSignals(task);
  if (sigs.some((s) => s.type === "ERROR")) return "error";
  if (sigs.some((s) => s.type === "FINAL")) return "final";
  return "open";
}

function taskTitle(task: TaskGroup): string {
  return task.hint || task.taskSig?.directed?.id || task.trace.slice(0, 16);
}

/** Render one signal node (and its lineage) as indented plain text with the
 *  full envelope expanded. */
function sigToText(node: SigNode, depth: number, out: string[]): void {
  const pad = "  ".repeat(depth);
  const s = node.sig;
  out.push(`${pad}- ${s.type}  ${s.directed?.id || "\u2014"}  ${safeTime(s.ts)}`);
  const json = JSON.stringify(s, null, 2)
    .split("\n")
    .map((l) => `${pad}    ${l}`)
    .join("\n");
  out.push(json);
  for (const c of node.children) sigToText(c, depth + 1, out);
}

/** Full plain-text dump of a task subtree: every signal, every envelope. */
function taskToText(task: TaskGroup, depth: number, out: string[]): void {
  const pad = "  ".repeat(depth);
  const label = depth > 0 || task.depth > 0 ? "SUBTASK" : "TASK";
  out.push("");
  out.push(`${pad}${"=".repeat(72 - pad.length)}`);
  out.push(`${pad}${label}: ${taskTitle(task)}`);
  out.push(`${pad}trace:    ${task.trace}`);
  out.push(`${pad}status:   ${taskStatus(task)}`);
  out.push(`${pad}duration: ${fmtMs(taskDuration(task))}`);
  out.push(`${pad}signals:  ${task.count} own / ${task.subtreeCount} subtree`);
  out.push(`${pad}${"=".repeat(72 - pad.length)}`);
  // The TASK envelope itself — the tree view peels it off (it *is* the header),
  // but an export that drops it loses the run's root and its dispatch metadata.
  if (task.taskSig) sigToText({ sig: task.taskSig, children: [] }, depth + 1, out);
  for (const n of ownSignalForest(task)) sigToText(n, depth + 1, out);
  for (const c of task.children) taskToText(c, depth + 1, out);
}

function slugify(s: string): string {
  return (
    s
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 48) || "task"
  );
}

function downloadTask(task: TaskGroup): void {
  const out: string[] = [
    `Cosmonapse \u00b7 Prism signal trace`,
    `exported: ${new Date().toISOString()}`,
  ];
  taskToText(task, 0, out);
  const blob = new Blob([out.join("\n") + "\n"], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${slugify(taskTitle(task))}-${task.trace.slice(0, 8)}.txt`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

export function SignalList({ signals }: Props) {
  const grouped = useMemo(() => groupSignals(signals), [signals]);
  // Collapsed group keys (default expanded). Full-envelope keys (default off).
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());
  const [full, setFull] = useState<Set<string>>(() => new Set());

  const toggle = (setter: typeof setCollapsed, key: string) =>
    setter((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

  const renderSig = (node: SigNode, depth: number): React.ReactNode => {
    const s = node.sig;
    const c = colorFor(s.type);
    const sKey = `s:${s.id}`;
    const fKey = `f:${s.id}`;
    const hasKids = node.children.length > 0;
    const kidsOpen = !collapsed.has(sKey);
    const isFull = full.has(fKey);
    return (
      <div key={s.id || `${s.trace_id}:${s.ts}:${depth}`} style={{ marginLeft: depth ? 18 : 0 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 7, padding: "3px 0" }}>
          <span
            onClick={() => hasKids && toggle(setCollapsed, sKey)}
            style={{
              width: 12,
              flexShrink: 0,
              textAlign: "center",
              color: C.textFaint, fontWeight: 600,
              fontSize: 12,
              cursor: hasKids ? "pointer" : "default",
            }}
          >
            {hasKids ? (kidsOpen ? "▼" : "▶") : "·"}
          </span>
          <span style={{ width: 8, height: 8, borderRadius: "50%", background: c, boxShadow: `0 0 5px ${c}`, flexShrink: 0 }} />
          <span style={{ color: c, fontFamily: MONO, fontSize: 14, fontWeight: 600, minWidth: 104, flexShrink: 0 }}>
            {s.type}
          </span>
          <span
            style={{
              color: C.textDim, fontWeight: 600,
              fontFamily: MONO,
              fontSize: 13.5,
              whiteSpace: "nowrap",
              overflow: "hidden",
              textOverflow: "ellipsis",
              flex: 1,
              minWidth: 0,
            }}
          >
            {s.directed?.id || "—"}
          </span>
          <span style={{ color: C.textFaint, fontWeight: 600, fontFamily: MONO, fontSize: 13, flexShrink: 0 }}>{safeTime(s.ts)}</span>
          <span
            onClick={() => toggle(setFull, fKey)}
            style={{
              color: isFull ? C.accent2 : C.textFaint,
              fontFamily: MONO,
              fontSize: 13,
              flexShrink: 0,
              cursor: "pointer",
              textDecoration: "underline",
              textDecorationColor: "rgba(var(--fg-rgb), 0.2)",
              textUnderlineOffset: 2,
            }}
          >
            {isFull ? "hide" : "expand"}
          </span>
        </div>
        {isFull && <pre style={preStyle}>{JSON.stringify(s, null, 2)}</pre>}
        {hasKids && kidsOpen && node.children.map((c2) => renderSig(c2, depth + 1))}
      </div>
    );
  };

  const renderTask = (task: TaskGroup): React.ReactNode => {
    const tKey = `t:${task.trace}`;
    const open = !collapsed.has(tKey);
    const sigs = ownSignals(task);
    const hasFinal = sigs.some((s) => s.type === "FINAL");
    const hasError = sigs.some((s) => s.type === "ERROR");
    const status = hasError ? "error" : hasFinal ? "final" : "open";
    const statusColor = hasError ? colorFor("ERROR") : hasFinal ? colorFor("FINAL") : C.textFaint;
    const forest = ownSignalForest(task);

    return (
      <div
        key={task.trace}
        style={{
          marginLeft: task.depth ? 16 : 0,
          borderLeft: task.depth ? `1px solid ${C.border}` : "none",
          paddingLeft: task.depth ? 16 : 0,
        }}
      >
        <div
          onClick={() => toggle(setCollapsed, tKey)}
          style={{
            display: "flex",
            alignItems: "center",
            gap: 10,
            padding: "9px 12px",
            marginTop: 8,
            background: C.bgCard,
            border: "1px solid " + C.border,
            borderLeft: `3px solid ${colorFor("TASK")}`,
            borderRadius: 10,
            cursor: "pointer",
            userSelect: "none",
          }}
        >
          <span style={{ color: C.textFaint, fontWeight: 600, fontSize: 13, width: 10, flexShrink: 0 }}>{open ? "▼" : "▶"}</span>
          <Tag color={colorFor("TASK")}>{task.depth > 0 ? "subtask" : "task"}</Tag>
          <span
            style={{
              fontFamily: MONO,
              fontSize: 14.5,
              color: C.text,
              whiteSpace: "nowrap",
              overflow: "hidden",
              textOverflow: "ellipsis",
              flex: 1,
              minWidth: 40,
            }}
          >
            {task.hint || task.taskSig?.directed?.id || task.trace.slice(0, 16)}
          </span>
          <Badge>{fmtMs(taskDuration(task))}</Badge>
          <Badge>{task.subtreeCount} sig{task.subtreeCount === 1 ? "" : "s"}</Badge>
          {task.children.length > 0 && (
            <Badge>{task.children.length} child{task.children.length === 1 ? "" : "ren"}</Badge>
          )}
          <span style={{ fontFamily: MONO, fontSize: 13, color: statusColor, flexShrink: 0 }}>● {status}</span>
          <span
            title="Download this task's full trace as text"
            onClick={(e) => {
              e.stopPropagation();
              downloadTask(task);
            }}
            style={{
              fontFamily: MONO,
              fontSize: 12.5,
              color: C.textFaint,
              border: "1px solid " + C.border,
              background: "rgba(var(--fg-rgb), 0.04)",
              borderRadius: 5,
              padding: "1px 7px",
              flexShrink: 0,
              cursor: "pointer",
              whiteSpace: "nowrap",
            }}
          >
            ↓ txt
          </span>
        </div>

        {open && (
          <>
            {/* child tasks first — the next grouping level down */}
            {task.children.map(renderTask)}
            {/* then this task's own signal lineage */}
            {forest.length > 0 && (
              <div style={{ marginLeft: 20, marginTop: 2, marginBottom: 4 }}>
                {forest.map((n) => renderSig(n, 0))}
              </div>
            )}
          </>
        )}
      </div>
    );
  };

  return (
    <div
      style={{
        position: "absolute",
        top: 64,
        left: 0,
        right: 0,
        bottom: 0,
        overflowY: "auto",
        padding: "24px 32px 48px",
        background: "var(--bg-view)",
      }}
    >
      <div style={{ maxWidth: 1000, margin: "0 auto" }}>
        {grouped.roots.length === 0 && (
          <div style={{ textAlign: "center", color: C.textFaint, fontWeight: 600, fontSize: 15, padding: 64 }}>
            Waiting for tasks…
          </div>
        )}
        {grouped.roots.map(renderTask)}

        {grouped.lifecycle.length > 0 && (
          <div style={{ marginTop: 20, opacity: 0.7 }}>
            <div style={{ fontFamily: MONO, fontSize: 13.5, color: C.textFaint, fontWeight: 600, letterSpacing: "0.1em", textTransform: "uppercase", marginBottom: 6 }}>
              lifecycle · {grouped.lifecycle.length}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function Tag({ color, children }: { color: string; children: React.ReactNode }) {
  return (
    <span
      style={{
        fontFamily: MONO,
        fontSize: 12.5,
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
      {children}
    </span>
  );
}

function Badge({ children }: { children: React.ReactNode }) {
  return (
    <span
      style={{
        fontFamily: MONO,
        fontSize: 13,
        color: C.textDim, fontWeight: 600,
        background: "rgba(var(--fg-rgb), 0.04)",
        border: "1px solid " + C.border,
        borderRadius: 5,
        padding: "1px 7px",
        flexShrink: 0,
        whiteSpace: "nowrap",
      }}
    >
      {children}
    </span>
  );
}

const preStyle: React.CSSProperties = {
  margin: "4px 0 6px 20px",
  padding: 10,
  background: "var(--bg-well)",
  border: "1px solid var(--border)",
  borderRadius: 8,
  color: "var(--text-dim)",
  fontSize: 13,
  fontFamily: MONO,
  whiteSpace: "pre-wrap",
  wordBreak: "break-all",
  maxHeight: 320,
  overflowY: "auto",
};

function safeTime(t: string): string {
  const d = new Date(t);
  return isNaN(d.getTime()) ? t : d.toISOString().slice(11, 23);
}
