import { useState } from "react";
import type { CSSProperties } from "react";
import { C, MONO } from "../theme";
import type { ImportWarning } from "../types";

/**
 * What Genesis can't do with this particular project.
 *
 * Opening a project someone else laid out is the case where Genesis's
 * assumptions can quietly not hold - no brain.py to wire into, components
 * outside the folders it reads, a module that builds its component in a
 * factory. None of that is worth blocking on, and all of it is worth saying
 * out loud, because the alternative is a button that appears to work and
 * doesn't.
 */
export function ImportNotes({ warnings }: { warnings: ImportWarning[] }) {
  const [dismissed, setDismissed] = useState<Set<string>>(new Set());
  const [open, setOpen] = useState(false);

  const live = warnings.filter((w) => !dismissed.has(w.id));
  if (live.length === 0) return null;

  return (
    <div style={barStyle}>
      <span style={{ color: C.effector, fontSize: 13.5, fontFamily: MONO, flexShrink: 0 }}>
        {live.length} note{live.length === 1 ? "" : "s"} about this project
      </span>

      {!open && (
        <span
          style={{
            fontSize: 13.5,
            color: C.textDim, fontWeight: 600,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          {live[0].text}
        </span>
      )}

      <div style={{ marginLeft: "auto", display: "flex", gap: 7, flexShrink: 0 }}>
        <button onClick={() => setOpen((o) => !o)} style={ghost}>
          {open ? "collapse" : live.length > 1 ? `show all ${live.length}` : "show"}
        </button>
        <button
          onClick={() => setDismissed(new Set(warnings.map((w) => w.id)))}
          style={ghost}
        >
          dismiss
        </button>
      </div>

      {open && (
        <div style={{ flexBasis: "100%", marginTop: 4 }}>
          {live.map((w) => (
            <div key={w.id} style={noteStyle}>
              <span style={{ flex: 1 }}>{w.text}</span>
              <span
                onClick={() => setDismissed((d) => new Set(d).add(w.id))}
                style={{ cursor: "pointer", color: C.textFaint, fontWeight: 600, paddingLeft: 10 }}
              >
                ×
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

const barStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 10,
  flexWrap: "wrap",
  padding: "8px 20px",
  borderBottom: "1px solid var(--border)",
  background: "rgba(var(--effector-rgb), 0.06)",
};

const noteStyle: CSSProperties = {
  display: "flex",
  alignItems: "flex-start",
  fontSize: 14,
  color: "var(--text-dim)",
  lineHeight: 1.55,
  padding: "7px 0",
  borderTop: "1px solid var(--border)",
};

const ghost: CSSProperties = {
  background: "transparent",
  border: "1px solid var(--border)",
  borderRadius: 7,
  color: "var(--text-dim)",
  padding: "3px 9px",
  fontSize: 13,
  fontFamily: MONO,
  cursor: "pointer",
};
