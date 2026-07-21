import { useMemo, useState } from "react";
import { C, MONO, colorFor } from "../theme";
import { groupSignals, type TaskGroup } from "../grouping";
import type { Signal } from "../types";

interface Props {
  open: boolean;
  width: number;
  signals: Signal[];
  selected: Signal | null;
  onSelect: (sig: Signal | null) => void;
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

  // Render one task and, beneath it, its pathways and nested child tasks.
  const renderTask = (task: TaskGroup) => {
    const tKey = `t:${task.trace}`;
    const tCollapsed = collapsed.has(tKey);
    const base = task.depth * 16;
    return (
      <div key={tKey}>
        <GroupHeader
          level={0}
          indent={base}
          collapsed={tCollapsed}
          color={colorFor("TASK")}
          tag={task.depth > 0 ? "subtask" : "task"}
          title={task.trace.slice(0, 16)}
          hint={task.hint}
          count={task.subtreeCount}
          onClick={() => toggleCollapsed(tKey)}
        />
        {!tCollapsed && (
          <>
            {task.pathways.map((pw) => {
              const pKey = `p:${task.trace}:${pw.key}`;
              const pCollapsed = collapsed.has(pKey);
              return (
                <div key={pKey}>
                  <GroupHeader
                    level={1}
                    indent={base}
                    collapsed={pCollapsed}
                    color={C.accent}
                    title={pw.label}
                    approx={pw.approx}
                    count={pw.signals.length}
                    onClick={() => toggleCollapsed(pKey)}
                  />
                  {!pCollapsed && pw.signals.map((sig, i) => renderRow(sig, i, 26 + base))}
                </div>
              );
            })}
            {task.children.map(renderTask)}
          </>
        )}
      </div>
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
            {groups.roots.map(renderTask)}

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
function GroupHeader({ level, indent = 0, collapsed, color, tag, title, hint, count, approx, onClick }: {
  level: 0 | 1;
  indent?: number;
  collapsed: boolean;
  color: string;
  tag?: string;
  title: string;
  hint?: string;
  count: number;
  approx?: boolean;
  onClick: () => void;
}) {
  const padLeft = (level === 0 ? 16 : 28) + indent;
  return (
    <div
      onClick={onClick}
      title={approx ? "Linked by time - this engram op was dispatched on a separate trace" : undefined}
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        padding: level === 0 ? `9px 16px 9px ${padLeft}px` : `6px 16px 6px ${padLeft}px`,
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
