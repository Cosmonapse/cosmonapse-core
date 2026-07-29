import { useEffect, useState } from "react";
import type { CSSProperties } from "react";
import { ensureHelpers, readFile, writeFile } from "../api";
import { C, MONO } from "../theme";
import type { InitError } from "../types";
import { CodeEditor } from "./CodeEditor";

/**
 * The one traditional editor in the Code tab.
 *
 * Components are structured because they're protocol surfaces - an identity
 * and a set of decorators. helpers.py is the opposite: ordinary Python that
 * every component can import, with no protocol involvement at all. So it
 * gets an ordinary editor. Entries run from the project root, which is what
 * makes "from helpers import ..." resolve from neurons/, effector/ and
 * engram/ alike.
 */
export function HelpersEditor({
  projectPath,
  exists,
  onCreated,
}: {
  projectPath: string;
  exists: boolean;
  onCreated: () => void;
}) {
  const [text, setText] = useState<string | null>(null);
  const [saved, setSaved] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    if (!exists) {
      setText(null);
      return;
    }
    readFile(projectPath, "helpers.py")
      .then((r) => {
        if (cancelled) return;
        setText(r.text);
        setSaved(r.text);
        setError(null);
      })
      .catch((e) => !cancelled && setError((e as InitError).error || "Couldn't read helpers.py."));
    return () => {
      cancelled = true;
    };
  }, [projectPath, exists]);

  async function create() {
    setBusy(true);
    try {
      const r = await ensureHelpers(projectPath);
      setText(r.text);
      setSaved(r.text);
      onCreated();
    } catch (e) {
      setError((e as InitError).error || "Couldn't create helpers.py.");
    } finally {
      setBusy(false);
    }
  }

  async function save() {
    if (text === null) return;
    setBusy(true);
    setError(null);
    try {
      await writeFile(projectPath, "helpers.py", text);
      setSaved(text);
      setNote("Saved");
      setTimeout(() => setNote(null), 2500);
    } catch (e) {
      setError((e as InitError).error || "That wouldn't parse - nothing was written.");
    } finally {
      setBusy(false);
    }
  }

  if (!exists && text === null) {
    return (
      <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", padding: 40 }}>
        <div style={{ maxWidth: 460, textAlign: "center" }}>
          <div style={{ fontSize: 15, color: C.text, marginBottom: 10, fontFamily: MONO }}>
            No helpers.py yet
          </div>
          <p style={{ fontSize: 14.5, color: C.textDim, fontWeight: 600, lineHeight: 1.6, marginBottom: 18 }}>
            One shared module of plain Python that every Neuron, Engram and Effector in this
            project can import. Nothing in it is a protocol surface — that's the point: the
            primitives stay about the bus, and ordinary logic stays ordinary.
          </p>
          <button onClick={create} disabled={busy} style={primary}>
            {busy ? "Creating…" : "Create helpers.py"}
          </button>
        </div>
      </div>
    );
  }

  const dirty = text !== null && text !== saved;

  return (
    <div style={{ flex: 1, display: "flex", flexDirection: "column", minHeight: 0 }}>
      <div style={barStyle}>
        <span style={{ fontFamily: MONO, fontSize: 13.5, color: C.text }}>helpers.py</span>
        <span style={{ fontSize: 13, color: C.textFaint, fontWeight: 600, }}>
          importable from every component — <code>from helpers import …</code>
        </span>
        {dirty && <span style={{ fontSize: 13, color: C.effector }}>· unsaved</span>}
        {note && <span style={{ fontSize: 13, color: C.okSoft }}>· {note}</span>}
        <div style={{ marginLeft: "auto", display: "flex", gap: 7 }}>
          {dirty && (
            <button onClick={() => setText(saved)} style={ghost}>
              revert
            </button>
          )}
          <button onClick={save} disabled={!dirty || busy} style={saveBtn(dirty && !busy)}>
            {busy ? "saving…" : "save"}
          </button>
        </div>
      </div>

      {error && <div style={errStyle}>{error}</div>}

      <div style={{ flex: 1, minHeight: 0, padding: 14 }}>
        <CodeEditor value={text ?? ""} onChange={setText} fill />
      </div>
    </div>
  );
}

const barStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 10,
  padding: "9px 16px",
  borderBottom: "1px solid var(--border)",
};

const ghost: CSSProperties = {
  background: "transparent",
  border: "1px solid var(--border)",
  borderRadius: 7,
  color: "var(--text-dim)",
  padding: "4px 10px",
  fontSize: 13,
  fontFamily: MONO,
  cursor: "pointer",
};

function saveBtn(on: boolean): CSSProperties {
  return {
    ...ghost,
    color: on ? C.accent2 : C.textFaint,
    borderColor: on ? "rgba(var(--accent2-rgb), 0.4)" : C.border,
    background: on ? "rgba(var(--accent2-rgb), 0.1)" : "transparent",
    cursor: on ? "pointer" : "default",
  };
}

const primary: CSSProperties = {
  padding: "10px 18px",
  borderRadius: 9,
  border: "none",
  background: "linear-gradient(135deg, var(--accent), var(--accent2))",
  color: "var(--on-primary)",
  fontWeight: 600,
  fontSize: 15,
  cursor: "pointer",
};

const errStyle: CSSProperties = {
  margin: "12px 14px 0",
  fontSize: 14,
  color: "var(--accent3)",
  lineHeight: 1.5,
  background: "rgba(var(--accent3-rgb), 0.07)",
  border: "1px solid rgba(var(--accent3-rgb), 0.25)",
  borderRadius: 8,
  padding: "10px 12px",
};
