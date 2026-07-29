import { useState } from "react";
import type { CSSProperties } from "react";
import { C, MONO } from "../theme";
import type { Field, FieldSpec } from "../types";

/**
 * One row of a declaration config form.
 *
 * The widget follows the value's *type*, which comes from the AST rather
 * than from a guess: a string literal gets a text box, a list of string
 * literals gets chips, and anything that isn't a literal at all - an
 * EngramBinding list, a custom parser - is shown read-only as the source it
 * is. A text input that pretended to edit "[EngramBinding(name="notes")]"
 * would round-trip it into a string literal and quietly break the module.
 */
export function FieldInput({
  field,
  spec,
  names,
  onChange,
  onRemove,
}: {
  field: Field;
  spec?: FieldSpec;
  /** Module-level async function names, for "name"-typed fields. */
  names: string[];
  onChange: (next: Field) => void;
  onRemove?: () => void;
}) {
  const label = spec?.blurb;
  const required = spec?.required;

  return (
    <div style={{ marginBottom: 14 }}>
      <div style={{ display: "flex", alignItems: "baseline", gap: 8, marginBottom: 5 }}>
        <label style={labelStyle}>{field.name}</label>
        {required && <span style={{ fontSize: 12, color: C.effector }}>required</span>}
        {field.type === "expr" && (
          <span style={{ fontSize: 12, color: C.textFaint, fontWeight: 600, }}>expression · edit in the file</span>
        )}
        {onRemove && !required && (
          <span onClick={onRemove} style={{ marginLeft: "auto", fontSize: 13, color: C.textFaint, fontWeight: 600, cursor: "pointer" }}>
            remove
          </span>
        )}
      </div>

      {field.type === "string_list" ? (
        <Chips
          value={(field.value as string[]) ?? []}
          suggest={spec?.suggest ?? []}
          onChange={(v) => onChange({ ...field, value: v })}
        />
      ) : field.type === "name" ? (
        <NameSelect
          value={String(field.value ?? "")}
          names={names}
          onChange={(v) => onChange({ ...field, value: v })}
        />
      ) : field.type === "bool" ? (
        <div style={{ display: "flex", gap: 6 }}>
          {[true, false].map((b) => (
            <button
              key={String(b)}
              onClick={() => onChange({ ...field, value: b })}
              style={pillStyle(field.value === b)}
            >
              {b ? "True" : "False"}
            </button>
          ))}
        </div>
      ) : field.type === "number" ? (
        <input
          type="number"
          value={String(field.value ?? "")}
          onChange={(e) => onChange({ ...field, value: Number(e.target.value) })}
          style={inputStyle}
        />
      ) : field.type === "expr" ? (
        <div style={{ ...inputStyle, color: C.textDim, fontWeight: 600, whiteSpace: "pre-wrap", cursor: "default" }}>
          {String(field.value ?? "")}
        </div>
      ) : (
        // string, or a None the form lets you fill in
        <>
          <input
            value={field.value === null ? "" : String(field.value)}
            placeholder={spec?.placeholder || (field.type === "none" ? "None" : "")}
            list={spec?.suggest?.length ? `sug-${field.name}` : undefined}
            onChange={(e) => {
              const v = e.target.value;
              onChange(v === "" ? { ...field, type: "none", value: null } : { ...field, type: "string", value: v });
            }}
            style={inputStyle}
          />
          {!!spec?.suggest?.length && (
            <datalist id={`sug-${field.name}`}>
              {spec.suggest.map((s) => (
                <option key={s} value={s} />
              ))}
            </datalist>
          )}
        </>
      )}

      {label && <div style={helpStyle}>{label}</div>}
    </div>
  );
}

/** A list of strings as removable chips plus a free-text add box. */
function Chips({
  value,
  suggest,
  onChange,
}: {
  value: string[];
  suggest: string[];
  onChange: (v: string[]) => void;
}) {
  const [draft, setDraft] = useState("");
  function add(v: string) {
    const t = v.trim();
    if (!t || value.includes(t)) return;
    onChange([...value, t]);
    setDraft("");
  }
  const unused = suggest.filter((s) => !value.includes(s));
  return (
    <div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: value.length ? 8 : 0 }}>
        {value.map((v) => (
          <span key={v} style={chipStyle}>
            {v}
            <span
              onClick={() => onChange(value.filter((x) => x !== v))}
              style={{ cursor: "pointer", color: C.textFaint, fontWeight: 600, marginLeft: 6 }}
            >
              ×
            </span>
          </span>
        ))}
      </div>
      <input
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === ",") {
            e.preventDefault();
            add(draft);
          }
        }}
        onBlur={() => add(draft)}
        placeholder="add one, Enter to confirm"
        style={inputStyle}
      />
      {unused.length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 6 }}>
          {unused.map((s) => (
            <span key={s} onClick={() => add(s)} style={{ ...chipStyle, cursor: "pointer", opacity: 0.55 }}>
              + {s}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

/** A reference to a function in this module - pick one, or type a name. */
function NameSelect({
  value,
  names,
  onChange,
}: {
  value: string;
  names: string[];
  onChange: (v: string) => void;
}) {
  const known = names.includes(value);
  return (
    <div style={{ display: "flex", gap: 6 }}>
      <select
        value={known ? value : "__other__"}
        onChange={(e) => e.target.value !== "__other__" && onChange(e.target.value)}
        style={{ ...inputStyle, flex: 1 }}
      >
        {names.map((n) => (
          <option key={n} value={n}>
            {n}
          </option>
        ))}
        {!known && <option value="__other__">{value || "(not in this module)"}</option>}
      </select>
      {!known && (
        <input value={value} onChange={(e) => onChange(e.target.value)} style={{ ...inputStyle, flex: 1 }} />
      )}
    </div>
  );
}

export const inputStyle: CSSProperties = {
  width: "100%",
  background: "var(--bg-elev)",
  border: "1px solid var(--border)",
  borderRadius: 7,
  color: "var(--text)",
  padding: "7px 10px",
  fontSize: 14.5,
  fontFamily: MONO,
  outline: "none",
};

export const labelStyle: CSSProperties = {
  fontSize: 13.5,
  fontFamily: MONO,
  color: "var(--text)",
};

const helpStyle: CSSProperties = {
  fontSize: 13,
  color: "var(--text-faint)",
  marginTop: 5,
  lineHeight: 1.45,
};

const chipStyle: CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  fontSize: 13.5,
  fontFamily: MONO,
  color: "var(--text-dim)",
  background: "rgba(var(--fg-rgb), 0.04)",
  border: "1px solid var(--border)",
  borderRadius: 999,
  padding: "3px 9px",
};

function pillStyle(on: boolean): CSSProperties {
  return {
    ...chipStyle,
    cursor: "pointer",
    color: on ? C.accent2 : C.textDim,
    borderColor: on ? "rgba(var(--accent2-rgb), 0.4)" : C.border,
    background: on ? "rgba(var(--accent2-rgb), 0.1)" : "transparent",
  };
}
