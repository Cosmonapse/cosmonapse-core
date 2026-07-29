import { useEffect, useRef, useState } from "react";
import type { CSSProperties } from "react";
import { addComponent } from "../api";
import { C, MONO } from "../theme";
import type { ComponentKind, ComponentResult, InitError, ReceptorShape } from "../types";
import { cup, kindColor } from "./CanvasNode";

const KINDS: { kind: ComponentKind; label: string; blurb: string }[] = [
  { kind: "neuron", label: "Neuron", blurb: "thinks - an async fn behind an Axon" },
  { kind: "engram", label: "Engram", blurb: "remembers - a memory backend" },
  { kind: "effector", label: "Effector", blurb: "acts - a tool family" },
  { kind: "receptor", label: "Receptor", blurb: "listens - the edge a turn arrives at" },
];

/**
 * A Receptor needs a second choice the other primitives don't: which of the
 * three classes to write.
 *
 * It is asked here, once, because it is not switchable afterwards - the three
 * take different constructor keywords and expose different decorators, so
 * turning one into another is a rewrite rather than a toggle. `extra` marks
 * the two that need the optional FastAPI dependency.
 */
const RECEPTOR_TYPES: {
  shape: ReceptorShape;
  label: string;
  blurb: string;
  extra: boolean;
}[] = [
  { shape: "cli", label: "CLI", blurb: "a typed command becomes a TASK; argparse + REPL derived from its signature", extra: false },
  { shape: "api", label: "API", blurb: "one HTTP endpoint, all three dispatch modes", extra: true },
  { shape: "chat", label: "Chat", blurb: "one turn, one dispatch, plus a served page (voice optional)", extra: true },
];

/**
 * The canvas palette: pick a primitive, name it, and Genesis writes the
 * module (neurons/<name>.py, effector/<name>.py, engram/<name>.py) and
 * wires it into brain.py. The name is used verbatim as the component's id
 * on the bus, so the file, the node and the Signal all say the same thing.
 */
export function AddComponent({
  projectPath,
  onAdded,
}: {
  projectPath: string;
  onAdded: (result: ComponentResult) => void;
}) {
  const [kind, setKind] = useState<ComponentKind | null>(null);
  const [shape, setShape] = useState<ReceptorShape>("cli");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (kind) inputRef.current?.focus();
  }, [kind]);

  function close() {
    setKind(null);
    setShape("cli");
    setName("");
    setError(null);
  }

  async function submit() {
    if (!kind || !name.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      const result = await addComponent({
        path: projectPath,
        kind,
        name: name.trim(),
        // Sent only where it means something; the backend ignores it for the
        // other three kinds, whose template is decided by the kind alone.
        ...(kind === "receptor" ? { shape } : {}),
      });
      onAdded(result);
      close();
    } catch (e) {
      setError((e as InitError).error || "Couldn't create that component.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      style={{
        position: "absolute",
        left: 20,
        bottom: 20,
        zIndex: 4,
        background: "var(--bg-panel)",
        WebkitBackdropFilter: "blur(20px)",
        backdropFilter: "blur(20px)",
        border: `1px solid ${C.borderStrong}`,
        borderRadius: 12,
        padding: 12,
        // Four primitives now share this row - "Effector" and "Receptor" are
        // the widest labels and need the extra 40px to sit on one line.
        width: 300,
        boxShadow: "0 18px 50px rgba(var(--shadow-rgb), 0.45)",
      }}
    >
      <div
        style={{
          fontSize: 13,
          color: C.textFaint, fontWeight: 600,
          textTransform: "uppercase",
          letterSpacing: "0.08em",
          marginBottom: 10,
        }}
      >
        Add a component
      </div>

      <div style={{ display: "flex", gap: 6 }}>
        {KINDS.map((k) => {
          const on = kind === k.kind;
          const color = kindColor()[k.kind];
          return (
            <button
              key={k.kind}
              title={k.blurb}
              onClick={() => (on ? close() : (setKind(k.kind), setError(null)))}
              style={{
                flex: 1,
                display: "flex",
                flexDirection: "column",
                alignItems: "center",
                gap: 6,
                padding: "10px 2px",
                borderRadius: 10,
                cursor: "pointer",
                background: on ? color + "18" : "transparent",
                border: `1px solid ${on ? color + "66" : C.border}`,
                color: on ? color : C.textDim,
                fontSize: 13,
                fontFamily: MONO,
                transition: "all 0.15s",
              }}
            >
              <Glyph kind={k.kind} color={on ? color : C.textDim} />
              {k.label}
            </button>
          );
        })}
      </div>

      {kind === "receptor" && (
        <div style={{ marginTop: 10 }}>
          <div style={{ display: "flex", gap: 6 }}>
            {RECEPTOR_TYPES.map((t) => {
              const on = shape === t.shape;
              const color = kindColor().receptor;
              return (
                <button
                  key={t.shape}
                  title={t.blurb}
                  onClick={() => setShape(t.shape)}
                  style={{
                    flex: 1,
                    padding: "6px 4px",
                    borderRadius: 8,
                    cursor: "pointer",
                    background: on ? color + "18" : "transparent",
                    border: `1px solid ${on ? color + "66" : C.border}`,
                    color: on ? color : C.textDim,
                    fontSize: 13,
                    fontFamily: MONO,
                    transition: "all 0.15s",
                  }}
                >
                  {t.label}
                </button>
              );
            })}
          </div>
          {RECEPTOR_TYPES.find((t) => t.shape === shape)?.extra && (
            <div style={{ fontSize: 13, color: C.warn, margin: "7px 2px 0", lineHeight: 1.45 }}>
              needs <code style={{ fontFamily: MONO }}>pip install 'cosmonapse[receptor]'</code>
            </div>
          )}
        </div>
      )}

      {kind && (
        <div style={{ marginTop: 10 }}>
          <input
            ref={inputRef}
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") submit();
              if (e.key === "Escape") close();
            }}
            placeholder={PLACEHOLDER[kind]}
            style={{
              width: "100%",
              background: C.bgElev,
              border: `1px solid ${C.border}`,
              borderRadius: 8,
              color: C.text,
              padding: "8px 10px",
              fontSize: 14.5,
              fontFamily: MONO,
              outline: "none",
            }}
          />
          <div style={{ fontSize: 13, color: C.textFaint, fontWeight: 600, margin: "6px 2px 0" }}>
            lowercase-with-dashes · becomes {FOLDER[kind]}/{(name.trim() || "name").replace(/-/g, "_")}.py
          </div>
          {error && (
            <div style={{ fontSize: 13.5, color: C.accent3, margin: "8px 2px 0", lineHeight: 1.4 }}>
              {error}
            </div>
          )}
          <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
            <button
              onClick={submit}
              disabled={!name.trim() || busy}
              style={primaryStyle(!!name.trim() && !busy)}
            >
              {busy ? "Creating…" : "Create"}
            </button>
            <button onClick={close} style={ghostStyle}>
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

const PLACEHOLDER: Record<ComponentKind, string> = {
  neuron: "summarize-notes",
  effector: "http-tools",
  engram: "session-memory",
  receptor: "terminal",
};

const FOLDER: Record<ComponentKind, string> = {
  neuron: "neurons",
  effector: "effector",
  engram: "engram",
  receptor: "receptors",
};

/** Miniature of the canvas silhouette, so the button reads as the shape it makes. */
function Glyph({ kind, color }: { kind: ComponentKind; color: string }) {
  return (
    <svg width="18" height="18" viewBox="-10 -10 20 20">
      {kind === "neuron" && <circle r="7" fill="none" stroke={color} strokeWidth="1.5" />}
      {kind === "engram" && (
        <polygon points="0,-8 8,0 0,8 -8,0" fill="none" stroke={color} strokeWidth="1.5" />
      )}
      {kind === "effector" && (
        <polygon points="0,-8 6.93,4 -6.93,4" fill="none" stroke={color} strokeWidth="1.5" />
      )}
      {kind === "receptor" && (
        <path d={cup(8)} fill="none" stroke={color} strokeWidth="1.5" />
      )}
    </svg>
  );
}

function primaryStyle(enabled: boolean): CSSProperties {
  return {
    flex: 1,
    padding: "8px 12px",
    borderRadius: 8,
    border: "none",
    background: enabled ? `linear-gradient(135deg, ${C.accent}, ${C.accent2})` : C.bgElev,
    color: enabled ? C.onPrimary : C.textFaint,
    fontWeight: 600,
    fontSize: 14.5,
    cursor: enabled ? "pointer" : "default",
  };
}

const ghostStyle: CSSProperties = {
  padding: "8px 12px",
  borderRadius: 8,
  border: "1px solid var(--border)",
  background: "transparent",
  color: "var(--text-dim)",
  fontSize: 14.5,
  cursor: "pointer",
};
