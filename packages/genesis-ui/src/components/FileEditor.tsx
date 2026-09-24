import { useEffect, useState } from "react";
import type { CSSProperties } from "react";
import { readFile, writeFile } from "../api";
import { C, MONO } from "../theme";
import type { InitError } from "../types";
import { CodeEditor } from "./CodeEditor";

/**
 * A plain editor for any project file: brain.py, config.py, README.md, a
 * package's __init__.py, or a component module's raw source.
 *
 * Saving goes through POST /api/file, which is confined to the project and
 * refuses a .py file that doesn't parse, so nothing here can leave the
 * project unable to start on a SyntaxError. Whatever it can't check (a
 * wrong import, an attach line pointing at nothing) is the author's call,
 * the same as in any other editor.
 */
export function FileEditor({
  projectPath,
  file,
  note,
  onSaved,
}: {
  projectPath: string;
  file: string;
  /** One line under the file name: what this file is for, or a caution. */
  note?: string;
  /** After a successful write, so the canvas and sidebar can re-read. */
  onSaved?: () => void;
}) {
  const [text, setText] = useState<string | null>(null);
  const [saved, setSaved] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [flash, setFlash] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setText(null);
    setError(null);
    readFile(projectPath, file)
      .then((r) => {
        if (cancelled) return;
        setText(r.text);
        setSaved(r.text);
      })
      .catch((e) => !cancelled && setError((e as InitError).error || `Couldn't read ${file}.`));
    return () => {
      cancelled = true;
    };
  }, [projectPath, file]);

  const dirty = text !== null && text !== saved;

  async function save() {
    if (text === null || !dirty || busy) return;
    setBusy(true);
    setError(null);
    try {
      await writeFile(projectPath, file, text);
      setSaved(text);
      setFlash("Saved");
      setTimeout(() => setFlash(null), 2500);
      onSaved?.();
    } catch (e) {
      setError((e as InitError).error || "That wouldn't parse - nothing was written.");
    } finally {
      setBusy(false);
    }
  }

  function onKeyDown(e: React.KeyboardEvent) {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
      e.preventDefault();
      void save();
    }
  }

  return (
    <div style={{ flex: 1, display: "flex", flexDirection: "column", minHeight: 0, minWidth: 0 }} onKeyDown={onKeyDown}>
      <div style={barStyle}>
        <span style={{ fontFamily: MONO, fontSize: 13.5, color: C.text }}>{file}</span>
        {note && <span style={{ fontSize: 13, color: C.textFaint, fontWeight: 600 }}>{note}</span>}
        {dirty && <span style={{ fontSize: 13, color: C.effector }}>· unsaved</span>}
        {flash && <span style={{ fontSize: 13, color: C.okSoft }}>· {flash}</span>}
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
        {text === null && !error && (
          <div style={{ color: C.textFaint, fontWeight: 600, fontSize: 15 }}>Reading…</div>
        )}
        {text !== null && <CodeEditor value={text} onChange={setText} fill />}
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
