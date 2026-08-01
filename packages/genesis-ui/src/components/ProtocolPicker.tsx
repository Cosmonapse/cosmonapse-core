import { useMemo, useState } from "react";
import type { CSSProperties } from "react";
import { C, MONO } from "../theme";
import type { Catalogue, ProtocolGroup, ProtocolSpec } from "../types";

/**
 * "What can this node do?" - the whole answer, in one panel.
 *
 * Cosmonapse is a decorator/emitter model, so that question has an exact
 * answer: the decorators the declared object actually carries. Own
 * protocols come first (they're the component's own surface and depend on
 * what it is), then the Dendrite signal family it can defer onto its host,
 * grouped so ~27 entries stay findable. The list is read live off the SDK,
 * so it can't drift from what the code would accept.
 */
export function ProtocolPicker({
  catalogue,
  taken,
  onPick,
  onClose,
}: {
  catalogue: Catalogue;
  /** protocol names already on this component - marked, not hidden. */
  taken: Set<string>;
  onPick: (scope: "own" | "host", spec: ProtocolSpec) => void;
  onClose: () => void;
}) {
  const [q, setQ] = useState("");

  const filter = (groups: ProtocolGroup[]) => {
    const needle = q.trim().toLowerCase();
    if (!needle) return groups;
    return groups
      .map((g) => ({
        ...g,
        protocols: g.protocols.filter(
          (p) =>
            p.name.toLowerCase().includes(needle) ||
            p.blurb.toLowerCase().includes(needle),
        ),
      }))
      .filter((g) => g.protocols.length > 0);
  };

  const own = useMemo(() => filter(catalogue.own), [catalogue, q]);
  const host = useMemo(() => filter(catalogue.host), [catalogue, q]);
  const empty = own.length === 0 && host.length === 0;

  return (
    <div style={panelStyle}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}>
        <div style={{ fontSize: 14.5, fontFamily: MONO, color: C.text }}>Add behaviour</div>
        <span style={{ fontSize: 13, color: C.textFaint, fontWeight: 600, }}>
          every protocol this component can service
        </span>
        <button onClick={onClose} style={{ marginLeft: "auto", ...ghost }}>
          close
        </button>
      </div>

      <input
        autoFocus
        value={q}
        onChange={(e) => setQ(e.target.value)}
        placeholder="Filter protocols…"
        style={searchStyle}
      />

      <div style={{ maxHeight: 380, overflowY: "auto", marginTop: 12 }}>
        {catalogue.own.length === 0 && catalogue.own_empty_reason && (
          <div style={noteStyle}>{catalogue.own_empty_reason}</div>
        )}

        {own.map((g) => (
          <Group
            key={g.title}
            title={g.title}
            accent={C.accent2}
            group={g}
            taken={taken}
            onPick={(p) => onPick("own", p)}
          />
        ))}

        {host.length > 0 && (
          <div style={{ ...sectionStyle, marginTop: own.length ? 18 : 0 }}>
            Host protocols
            <span style={{ color: C.textFaint, fontWeight: 400, textTransform: "none", letterSpacing: 0 }}>
              {" "}
              · declared here, registered on the Dendrite that hosts this component
            </span>
          </div>
        )}
        {host.map((g) => (
          <Group
            key={g.title}
            title={g.title}
            accent={C.accent}
            group={g}
            taken={taken}
            onPick={(p) => onPick("host", p)}
          />
        ))}

        {empty && <div style={noteStyle}>Nothing matches “{q}”.</div>}
      </div>
    </div>
  );
}

function Group({
  title,
  accent,
  group,
  taken,
  onPick,
}: {
  title: string;
  accent: string;
  group: ProtocolGroup;
  taken: Set<string>;
  onPick: (p: ProtocolSpec) => void;
}) {
  return (
    <div style={{ marginBottom: 14 }}>
      <div style={{ ...sectionStyle, color: accent }}>{title}</div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(230px,1fr))", gap: 8 }}>
        {group.protocols.map((p) => {
          const on = taken.has(p.name);
          return (
            <div
              key={p.name}
              onClick={() => onPick(p)}
              style={{
                border: `1px solid ${C.border}`,
                borderRadius: 9,
                padding: "9px 11px",
                cursor: "pointer",
                background: "rgba(var(--fg-rgb), 0.015)",
              }}
              onMouseEnter={(e) => (e.currentTarget.style.borderColor = accent + "66")}
              onMouseLeave={(e) => (e.currentTarget.style.borderColor = C.border)}
            >
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <span style={{ fontFamily: MONO, fontSize: 14, color: C.text }}>{p.name}</span>
                {on && (
                  <span style={{ fontSize: 12, color: accent, opacity: 0.8 }}>· in use</span>
                )}
              </div>
              <div style={{ fontSize: 13, color: C.textFaint, fontWeight: 600, marginTop: 4, lineHeight: 1.45 }}>
                {p.blurb}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

const panelStyle: CSSProperties = {
  border: "1px solid var(--border-strong)",
  borderRadius: 12,
  background: "var(--bg-panel)",
  padding: 16,
  marginBottom: 18,
};

const sectionStyle: CSSProperties = {
  fontSize: 13,
  letterSpacing: "0.08em",
  textTransform: "uppercase",
  color: "var(--text-dim)",
  marginBottom: 8,
};

const searchStyle: CSSProperties = {
  width: "100%",
  background: "var(--bg-elev)",
  border: "1px solid var(--border)",
  borderRadius: 8,
  color: "var(--text)",
  padding: "8px 11px",
  fontSize: 14.5,
  fontFamily: MONO,
  outline: "none",
};

const noteStyle: CSSProperties = {
  fontSize: 14,
  color: "var(--text-dim)",
  lineHeight: 1.55,
  background: "rgba(var(--effector-rgb), 0.06)",
  border: "1px solid rgba(var(--effector-rgb), 0.25)",
  borderRadius: 8,
  padding: "10px 12px",
  marginBottom: 14,
};

const ghost: CSSProperties = {
  background: "transparent",
  border: "1px solid var(--border)",
  borderRadius: 7,
  color: "var(--text-dim)",
  padding: "4px 10px",
  fontSize: 13.5,
  fontFamily: MONO,
  cursor: "pointer",
};
