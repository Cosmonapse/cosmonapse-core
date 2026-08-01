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
  // A credential is masked unless asked for. The spec is authoritative, but
  // the name check catches the same thing on a keyword no table describes
  // yet - a form that shows one key in the clear teaches the wrong habit.
  const secret = spec?.secret ?? SECRET_NAME.test(field.name);
  const unset = isBlank(field);

  return (
    <div style={{ marginBottom: 14 }}>
      <div style={{ display: "flex", alignItems: "baseline", gap: 8, marginBottom: 5 }}>
        <label style={labelStyle}>{field.name}</label>
        {required && (
          <span style={{ fontSize: 12, color: unset ? C.warn : C.effector }}>
            {unset ? "required · not set" : "required"}
          </span>
        )}
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
      ) : secret ? (
        <SecretInput
          value={field.value === null ? "" : String(field.value)}
          placeholder={spec?.placeholder || "sk-…"}
          missing={required && unset}
          onChange={(v) =>
            onChange(v === "" ? { ...field, type: "none", value: null } : { ...field, type: "string", value: v })
          }
        />
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
            style={required && unset ? missingStyle : inputStyle}
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

/**
 * Keyword names that carry a credential, for the sources whose field table
 * doesn't say so itself. Kept deliberately broad - a false positive costs a
 * click on the reveal toggle, a false negative puts a live key on screen.
 */
const SECRET_NAME = /(^|_)(api_?key|access_?key|token|secret|password|passwd|pwd)(_|$)/i;

/** Whether a field is carrying nothing yet - an unfilled required row. */
export function isBlank(field: Field): boolean {
  if (field.type === "string_list") return ((field.value as string[]) ?? []).length === 0;
  if (field.type === "bool") return typeof field.value !== "boolean";
  return field.value === null || field.value === undefined || String(field.value).trim() === "";
}

/**
 * A credential box: dots by default, one click to read it back.
 *
 * The value still lives in the file in plain text - this is about the screen,
 * not about storage - so the toggle is an eye rather than anything that
 * pretends the key is encrypted.
 */
function SecretInput({
  value,
  placeholder,
  missing,
  onChange,
}: {
  value: string;
  placeholder: string;
  missing?: boolean;
  onChange: (v: string) => void;
}) {
  const [shown, setShown] = useState(false);
  return (
    <div style={{ position: "relative" }}>
      <input
        type={shown ? "text" : "password"}
        value={value}
        placeholder={placeholder}
        autoComplete="off"
        spellCheck={false}
        onChange={(e) => onChange(e.target.value)}
        style={{ ...(missing ? missingStyle : inputStyle), paddingRight: 38 }}
      />
      <span
        onClick={() => setShown((s) => !s)}
        title={shown ? "hide" : "show"}
        style={{
          position: "absolute",
          right: 8,
          top: "50%",
          transform: "translateY(-50%)",
          display: "flex",
          cursor: "pointer",
          color: shown ? C.accent2 : C.textFaint,
          padding: 2,
        }}
      >
        <Eye off={!shown} />
      </span>
    </div>
  );
}

function Eye({ off }: { off: boolean }) {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
      <path d="M2 12s3.6-6.5 10-6.5S22 12 22 12s-3.6 6.5-10 6.5S2 12 2 12z" />
      <circle cx="12" cy="12" r="2.8" />
      {off && <path d="M4 20 20 4" />}
    </svg>
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

/** An unfilled required box, so the form says so before the save fails. */
export const missingStyle: CSSProperties = {
  ...inputStyle,
  borderColor: "color-mix(in srgb, var(--warn) 50%, transparent)",
  background: "color-mix(in srgb, var(--warn) 7%, transparent)",
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
