import { useState } from "react";
import type { ReactNode } from "react";
import { initProject } from "../api";
import type { InitError, InitResult } from "../types";
import { C, MONO } from "../theme";
import { FolderBrowser } from "./FolderBrowser";
import { Logo } from "./Logo";

/**
 * Genesis step 1: name a new brain (AI agent/system), pick a folder, and
 * scaffold it with the same standard skeleton `cosmo init` produces
 * (config.py, neurons/, effector/, brain.py, demo.py, README.md).
 */
export function StartForm({
  onScaffolded,
}: {
  onScaffolded: (result: InitResult) => void;
}) {
  const [name, setName] = useState("cosmonapse-app");
  const [folder, setFolder] = useState("");
  const [namespace, setNamespace] = useState("demo");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);

  const canStart = name.trim().length > 0 && folder.trim().length > 0 && !busy;

  async function start(force = false) {
    setBusy(true);
    setError(null);
    setConflict(false);
    try {
      const result = await initProject({ name: name.trim(), path: folder, namespace: namespace.trim() || "demo", force });
      onScaffolded(result);
    } catch (e) {
      const err = e as InitError;
      setError(err.error || "Something went wrong scaffolding the project.");
      setConflict(!!err.exists);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      style={{
        minHeight: "100vh",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: 24,
      }}
    >
      <div
        style={{
          width: 480,
          maxWidth: "100%",
          background: C.bgCard,
          border: `1px solid ${C.border}`,
          borderRadius: 16,
          padding: 28,
          boxShadow: "0 20px 60px rgba(0,0,0,0.4)",
        }}
      >
        <div style={{ marginBottom: 22 }}>
          <Logo />
          <p style={{ color: C.textDim, fontSize: 13, marginTop: 10, lineHeight: 1.5 }}>
            Start a new brain: name the AI agent / system, choose where it lives, and
            Genesis scaffolds a runnable Cosmonapse project - one Synapse, a Neuron, an
            Effector - ready to grow.
          </p>
        </div>

        <Field label="Brain name">
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="cosmonapse-app"
            style={inputStyle}
          />
        </Field>

        <Field label="Folder location">
          <input
            value={folder}
            onChange={(e) => setFolder(e.target.value)}
            placeholder="/path/to/parent/folder"
            style={{ ...inputStyle, marginBottom: 10 }}
          />
          <FolderBrowser path={folder} onChange={setFolder} />
        </Field>

        <Field label="Namespace">
          <input
            value={namespace}
            onChange={(e) => setNamespace(e.target.value)}
            placeholder="demo"
            style={inputStyle}
          />
        </Field>

        {error && (
          <div
            style={{
              background: "rgba(244,113,182,0.08)",
              border: `1px solid rgba(244,113,182,0.3)`,
              color: C.accent3,
              borderRadius: 8,
              padding: "10px 12px",
              fontSize: 13,
              marginBottom: 16,
            }}
          >
            {error}
            {conflict && (
              <div style={{ marginTop: 8 }}>
                <button onClick={() => start(true)} style={secondaryBtnStyle}>
                  Overwrite existing files and continue
                </button>
              </div>
            )}
          </div>
        )}

        <button disabled={!canStart} onClick={() => start(false)} style={primaryBtnStyle(canStart)}>
          {busy ? "Scaffolding…" : "Start"}
        </button>
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div style={{ marginBottom: 18 }}>
      <label
        style={{
          display: "block",
          fontSize: 12,
          color: C.textFaint,
          textTransform: "uppercase",
          letterSpacing: "0.06em",
          marginBottom: 8,
        }}
      >
        {label}
      </label>
      {children}
    </div>
  );
}

const inputStyle = {
  width: "100%",
  background: C.bgElev,
  border: `1px solid ${C.border}`,
  borderRadius: 8,
  color: C.text,
  padding: "10px 12px",
  fontSize: 13,
  fontFamily: MONO,
  outline: "none",
};

function primaryBtnStyle(enabled: boolean) {
  return {
    width: "100%",
    padding: "12px 16px",
    borderRadius: 10,
    border: "none",
    background: enabled ? `linear-gradient(135deg, ${C.accent}, ${C.accent2})` : C.bgElev,
    color: enabled ? "#0a0b10" : C.textFaint,
    fontWeight: 600,
    fontSize: 14,
    cursor: enabled ? "pointer" : "default",
  };
}

const secondaryBtnStyle = {
  background: "transparent",
  border: `1px solid rgba(244,113,182,0.4)`,
  color: C.accent3,
  borderRadius: 6,
  padding: "6px 10px",
  fontSize: 12,
  cursor: "pointer",
};
