import { useEffect, useState } from "react";
import type { CSSProperties } from "react";
import { C, MONO } from "../theme";
import type { Behavior, Field, ProtocolSpec } from "../types";
import { CodeEditor } from "./CodeEditor";
import { inputStyle } from "./FieldInput";

export interface DraftBehavior {
  behavior_id: string | null;
  scope: "own" | "host";
  protocol: string;
  fn_name: string;
  signature: string;
  body: string;
  args: Field[];
  is_async: boolean;
  indent: boolean;
}

/** Turn an existing parsed behaviour into an editable draft. */
export function draftOf(b: Behavior): DraftBehavior {
  return {
    behavior_id: b.id,
    scope: b.scope,
    protocol: b.protocol,
    fn_name: b.fn_name,
    signature: b.signature,
    body: b.body,
    args: Object.entries(b.args).map(([name, f]) => ({ ...f, name })),
    is_async: b.is_async,
    indent: b.dedented,
  };
}

/** Turn a picked protocol into a new draft, pre-filled so it runs as written. */
export function draftFrom(scope: "own" | "host", spec: ProtocolSpec, taken: Set<string>): DraftBehavior {
  let name = spec.name.replace(/^(on_|detects_|before_)/, "") || spec.name;
  let n = 2;
  while (taken.has(name)) name = `${spec.name.replace(/^(on_|detects_|before_)/, "")}_${n++}`;
  return {
    behavior_id: null,
    scope,
    protocol: spec.name,
    fn_name: name,
    signature: spec.handler_args,
    body: spec.body,
    // Decorator args start out empty; only the ones the user fills are written.
    args: spec.decorator_args
      .filter((a) => a.required)
      .map((a) => ({ name: a.name, type: a.type, value: a.value })),
    is_async: true,
    indent: true,
  };
}

/**
 * One decorated behaviour, as a card: the decorator line (with its filters),
 * the handler signature, and a code box for the body. Save writes exactly
 * this block back into the module - nothing else in the file moves.
 */
export function BehaviorCard({
  draft,
  spec,
  target,
  dirty,
  busy,
  error,
  onChange,
  onSave,
  onRevert,
  onDelete,
}: {
  draft: DraftBehavior;
  spec?: ProtocolSpec;
  target: string;
  dirty: boolean;
  busy: boolean;
  error: string | null;
  onChange: (d: DraftBehavior) => void;
  onSave: () => void;
  onRevert: () => void;
  onDelete: () => void;
}) {
  const [showFilters, setShowFilters] = useState(draft.args.length > 0);
  useEffect(() => {
    if (draft.args.length > 0) setShowFilters(true);
  }, [draft.args.length]);

  const path = draft.scope === "host" ? `${target}.host.${draft.protocol}` : `${target}.${draft.protocol}`;
  const accent = draft.scope === "host" ? C.accent : C.accent2;
  const filterSpecs = spec?.decorator_args ?? [];

  function setArg(name: string, value: string | number | boolean | string[]) {
    const rest = draft.args.filter((a) => a.name !== name);
    // An emptied field means "don't write this argument at all". false is a
    // real value a user chose, so it is deliberately not treated as empty.
    const blank =
      value === "" || value === null || (Array.isArray(value) && value.length === 0);
    if (blank) {
      onChange({ ...draft, args: rest });
      return;
    }
    const type = filterSpecs.find((f) => f.name === name)?.type ?? "string";
    onChange({ ...draft, args: [...rest, { name, type, value }] });
  }

  return (
    <div
      style={{
        border: `1px solid ${dirty ? accent + "55" : C.border}`,
        borderRadius: 11,
        marginBottom: 14,
        background: "rgba(var(--fg-rgb), 0.012)",
        overflow: "hidden",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 10,
          padding: "9px 13px",
          borderBottom: `1px solid ${C.border}`,
        }}
      >
        <span style={{ fontFamily: MONO, fontSize: 14, color: accent }}>@{path}</span>
        <span style={{ fontSize: 12.5, color: C.textFaint, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.06em" }}>
          {draft.scope}
        </span>
        {dirty && <span style={{ fontSize: 13, color: C.effector }}>· unsaved</span>}
        <div style={{ marginLeft: "auto", display: "flex", gap: 7 }}>
          {filterSpecs.length > 0 && (
            <button onClick={() => setShowFilters((s) => !s)} style={ghost}>
              {showFilters ? "hide filters" : "filters"}
            </button>
          )}
          {dirty && (
            <button onClick={onRevert} style={ghost}>
              revert
            </button>
          )}
          <button onClick={onSave} disabled={!dirty || busy} style={saveStyle(dirty && !busy, accent)}>
            {busy ? "saving…" : "save"}
          </button>
          <button onClick={onDelete} style={{ ...ghost, color: C.accent3, borderColor: "rgba(var(--accent3-rgb), 0.3)" }}>
            delete
          </button>
        </div>
      </div>

      {showFilters && filterSpecs.length > 0 && (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit,minmax(180px,1fr))",
            gap: 10,
            padding: "11px 13px",
            borderBottom: `1px solid ${C.border}`,
            background: "var(--bg-well)",
          }}
        >
          {filterSpecs.map((f) => {
            const current = draft.args.find((a) => a.name === f.name);
            return (
              <div key={f.name}>
                <div style={{ fontSize: 13, fontFamily: MONO, color: C.textDim, fontWeight: 600, marginBottom: 4 }}>
                  {f.name}
                </div>
                {f.type === "bool" ? (
                  // A tri-state, because "unset" and "False" are different
                  // outcomes: unset writes no argument at all and lets the
                  // SDK default stand, False writes local=False explicitly.
                  <select
                    value={current === undefined ? "" : current.value ? "true" : "false"}
                    onChange={(e) =>
                      e.target.value === ""
                        ? setArg(f.name, "")
                        : setArg(f.name, e.target.value === "true")
                    }
                    style={inputStyle}
                  >
                    <option value="">unset</option>
                    <option value="true">True</option>
                    <option value="false">False</option>
                  </select>
                ) : f.type === "string_list" ? (
                  <input
                    type="text"
                    value={Array.isArray(current?.value) ? current.value.join(", ") : ""}
                    placeholder={f.required ? "required" : "comma, separated"}
                    onChange={(e) =>
                      setArg(
                        f.name,
                        e.target.value
                          .split(",")
                          .map((s) => s.trim())
                          .filter(Boolean),
                      )
                    }
                    style={inputStyle}
                  />
                ) : (
                  <input
                    type={f.type === "number" ? "number" : "text"}
                    value={current ? String(current.value ?? "") : ""}
                    placeholder={f.required ? "required" : "any"}
                    onChange={(e) =>
                      setArg(f.name, f.type === "number" ? Number(e.target.value) : e.target.value)
                    }
                    style={inputStyle}
                  />
                )}
                <div style={{ fontSize: 12.5, color: C.textFaint, fontWeight: 600, marginTop: 4 }}>{f.blurb}</div>
              </div>
            );
          })}
        </div>
      )}

      <div style={{ padding: "11px 13px" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 8, flexWrap: "wrap" }}>
          <span style={{ fontFamily: MONO, fontSize: 14, color: C.accent }}>
            {draft.is_async ? "async def" : "def"}
          </span>
          <input
            value={draft.fn_name}
            onChange={(e) => onChange({ ...draft, fn_name: e.target.value })}
            style={{ ...inputStyle, width: 150, padding: "4px 8px" }}
          />
          <span style={{ fontFamily: MONO, fontSize: 14.5, color: C.textDim, fontWeight: 600, }}>(</span>
          <input
            value={draft.signature}
            onChange={(e) => onChange({ ...draft, signature: e.target.value })}
            style={{ ...inputStyle, flex: 1, minWidth: 140, padding: "4px 8px" }}
          />
          <span style={{ fontFamily: MONO, fontSize: 14.5, color: C.textDim, fontWeight: 600, }}>):</span>
        </div>

        <CodeEditor
          value={draft.body}
          onChange={(body) => onChange({ ...draft, body })}
          minRows={3}
          maxRows={22}
          placeholder="..."
        />

        {spec?.blurb && (
          <div style={{ fontSize: 13, color: C.textFaint, fontWeight: 600, marginTop: 8, lineHeight: 1.5 }}>
            {spec.blurb}
          </div>
        )}
        {error && (
          <div
            style={{
              fontSize: 13.5,
              color: C.accent3,
              marginTop: 8,
              lineHeight: 1.5,
              background: "rgba(var(--accent3-rgb), 0.07)",
              border: "1px solid rgba(var(--accent3-rgb), 0.25)",
              borderRadius: 7,
              padding: "8px 10px",
            }}
          >
            {error}
          </div>
        )}
      </div>
    </div>
  );
}

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

function saveStyle(on: boolean, accent: string): CSSProperties {
  return {
    ...ghost,
    color: on ? accent : C.textFaint,
    borderColor: on ? accent + "55" : C.border,
    background: on ? accent + "14" : "transparent",
    cursor: on ? "pointer" : "default",
  };
}
