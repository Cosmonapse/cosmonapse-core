import { useEffect, useState } from "react";
import type { CSSProperties, ReactNode } from "react";
import { detect, initProject } from "../api";
import { forgetProject, loadRecents } from "../recents";
import { C, MONO } from "../theme";
import type { Detection, InitError, InitResult, RecentProject } from "../types";
import { FolderBrowser, pushRecent } from "./FolderBrowser";
import { kindColor } from "./CanvasNode";
import { GitProject } from "./GitProject";
import { Logo } from "./Logo";
import { ThemeToggle } from "./ThemeToggle";

/**
 * One screen for every way in.
 *
 * Two tabs, and they are the two genuinely different questions: is the
 * project already on this machine, or is it on GitHub? Everything else about
 * "where" is a folder, and Genesis answers that by looking rather than
 * asking - it probes each folder as you browse it, so the primary action
 * follows what is actually there. A folder that already holds a project
 * offers to open it, a folder of projects offers those, and anywhere else
 * offers to scaffold something new. You find out what a folder is by looking
 * at it, not by picking a mode up front.
 *
 * The Git tab is the one thing that could not be reduced to a folder, since
 * it needs an account before it can show you anything.
 */
export function StartScreen({
  onScaffolded,
  onOpen,
}: {
  onScaffolded: (result: InitResult) => void;
  onOpen: (path: string, name: string) => void;
}) {
  const [name, setName] = useState("cosmonapse-app");
  const [folder, setFolder] = useState("");
  const [namespace, setNamespace] = useState("demo");
  const [git, setGit] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const [probe, setProbe] = useState<Detection | null>(null);
  const [probing, setProbing] = useState(false);
  const [recents, setRecents] = useState<RecentProject[]>(loadRecents);
  const [mode, setMode] = useState<"local" | "git">("local");

  // Probe whatever folder the browser is sitting on. Debounced, because the
  // browser fires on every keystroke of its filter and every arrow press.
  useEffect(() => {
    if (!folder) {
      setProbe(null);
      return;
    }
    let cancelled = false;
    setProbing(true);
    const t = setTimeout(() => {
      detect(folder)
        .then((d) => !cancelled && setProbe(d))
        .catch(() => !cancelled && setProbe(null))
        .finally(() => !cancelled && setProbing(false));
    }, 180);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [folder]);

  async function create(force = false) {
    setBusy(true);
    setError(null);
    setConflict(false);
    try {
      const result = await initProject({
        name: name.trim(),
        path: folder,
        namespace: namespace.trim() || "demo",
        force,
        git,
      });
      pushRecent(folder);
      onScaffolded(result);
    } catch (e) {
      const err = e as InitError;
      setError(err.error || "Something went wrong scaffolding the project.");
      setConflict(!!err.exists);
    } finally {
      setBusy(false);
    }
  }

  const isProject = !!probe?.is_project;
  const canCreate = name.trim().length > 0 && folder.trim().length > 0 && !busy;

  return (
    <div style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center", padding: 24 }}>
      <div style={{ ...cardStyle, position: "relative" }}>
        <div style={{ position: "absolute", top: 18, right: 18 }}>
          <ThemeToggle />
        </div>
        <div style={{ marginBottom: 20 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <Logo size={30} />
            <span className="brand-word" style={{ fontWeight: 700, fontSize: 18 }}>
              Cosmonapse
            </span>
            <span style={{ color: C.textDim, fontWeight: 500, fontSize: 18 }}>Genesis</span>
          </div>
          <p style={{ color: C.textDim, fontWeight: 600, fontSize: 15, marginTop: 10, lineHeight: 1.55 }}>
            Start a new brain, open one you already have, or clone one from GitHub. Genesis lays
            it out as a canvas you can grow — one Synapse, its Neurons, Engrams and Effectors.
          </p>
        </div>

        <div style={{ display: "flex", gap: 6, marginBottom: 18 }}>
          {(["local", "git"] as const).map((m) => {
            const on = m === mode;
            return (
              <div
                key={m}
                onClick={() => setMode(m)}
                style={{
                  padding: "6px 16px",
                  borderRadius: 9,
                  cursor: "pointer",
                  fontFamily: MONO,
                  fontSize: 14.5,
                  color: on ? C.accent2 : C.textDim,
                  background: on ? "rgba(var(--accent2-rgb), 0.12)" : "transparent",
                  border: "1px solid " + (on ? "rgba(var(--accent2-rgb), 0.4)" : C.border),
                  transition: "all 0.15s",
                }}
              >
                {m === "local" ? "This computer" : "From git"}
              </div>
            );
          })}
        </div>

        {mode === "git" && <GitProject onOpen={onOpen} />}

        {mode === "local" && (
        <>
        {recents.length > 0 && (
          <Field label="Recent">
            <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
              {recents.map((r) => (
                <span key={r.path} title={r.path} style={recentChip}>
                  <span onClick={() => onOpen(r.path, r.name)} style={{ cursor: "pointer" }}>
                    {r.name}
                  </span>
                  <span
                    onClick={() => setRecents(forgetProject(r.path))}
                    style={{ cursor: "pointer", color: C.textFaint, fontWeight: 600, marginLeft: 7 }}
                  >
                    ×
                  </span>
                </span>
              ))}
            </div>
          </Field>
        )}

        <Field label="Folder">
          <input
            value={folder}
            onChange={(e) => setFolder(e.target.value)}
            placeholder="/path/to/folder"
            style={{ ...inputStyle, marginBottom: 10 }}
          />
          <FolderBrowser path={folder} onChange={setFolder} />
        </Field>

        {/* What Genesis makes of the folder you're standing in */}
        <Verdict
          probe={probe}
          probing={probing}
          onOpenChild={(path, childName) => onOpen(path, childName)}
        />

        {/* The new-brain fields only matter when there's nothing to open */}
        {!isProject && (
          <div style={{ display: "flex", gap: 12 }}>
            <div style={{ flex: 2 }}>
              <Field label="Brain name">
                <input
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="cosmonapse-app"
                  style={inputStyle}
                />
              </Field>
            </div>
            <div style={{ flex: 1 }}>
              <Field label="Namespace">
                <input
                  value={namespace}
                  onChange={(e) => setNamespace(e.target.value)}
                  placeholder="demo"
                  style={inputStyle}
                />
              </Field>
            </div>
          </div>
        )}

        {/* Genesis rewrites brain.py on every add and remove, so a repository
            is the undo for most of what it does. Defaulted on, but still a
            tick: scaffolding inside a repo somebody already has is common
            enough that starting a second one silently would be wrong. The
            .gitignore is written either way - it costs nothing, and it is
            wanted whether or not this project is the repository root. */}
        {!isProject && (
          <label style={gitToggleStyle}>
            <input
              type="checkbox"
              checked={git}
              onChange={(e) => setGit(e.target.checked)}
              style={{ accentColor: C.accent2 }}
            />
            <span style={{ fontFamily: MONO, fontSize: 14, color: C.text }}>
              Start a git repository
            </span>
            <span style={{ fontSize: 13, color: C.textFaint, fontWeight: 600 }}>
              first commit included · skipped if this folder is already in one
            </span>
          </label>
        )}

        {error && (
          <div style={errorStyle}>
            {error}
            {conflict && (
              <div style={{ marginTop: 8 }}>
                <button onClick={() => create(true)} style={secondaryBtnStyle}>
                  Overwrite existing files and continue
                </button>
              </div>
            )}
          </div>
        )}

        {isProject && probe ? (
          <button onClick={() => onOpen(probe.path, probe.name)} style={primaryBtnStyle(true)}>
            Open {probe.name}
          </button>
        ) : (
          <button disabled={!canCreate} onClick={() => create(false)} style={primaryBtnStyle(canCreate)}>
            {busy ? "Scaffolding…" : folder ? `Create ${name.trim() || "…"} here` : "Create"}
          </button>
        )}
        </>
        )}
      </div>
    </div>
  );
}

/** The probe result, said plainly: what's here, or why there's nothing to open. */
function Verdict({
  probe,
  probing,
  onOpenChild,
}: {
  probe: Detection | null;
  probing: boolean;
  onOpenChild: (path: string, name: string) => void;
}) {
  if (!probe) {
    return (
      <div style={{ ...verdictStyle, color: C.textFaint, fontWeight: 600, }}>
        {probing ? "Looking…" : "Pick a folder to see what's in it."}
      </div>
    );
  }

  if (probe.is_project) {
    const { neurons = 0, engrams = 0, effectors = 0, receptors = 0 } = probe.counts;
    const total = neurons + engrams + effectors + receptors;
    return (
      <div style={{ ...verdictStyle, borderColor: "rgba(var(--accent2-rgb), 0.3)", background: "rgba(var(--accent2-rgb), 0.05)" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <span style={{ color: C.accent2, fontFamily: MONO, fontSize: 14 }}>
            {probe.name}
          </span>
          <span style={{ color: C.textDim, fontWeight: 600, fontSize: 13.5 }}>
            is a Cosmonapse project
            {!probe.scaffolded && " (not the standard skeleton)"}
          </span>
        </div>
        <div style={{ display: "flex", gap: 6, marginTop: 8, flexWrap: "wrap" }}>
          {total === 0 ? (
            <span style={{ fontSize: 13.5, color: C.textFaint, fontWeight: 600, }}>no components yet</span>
          ) : (
            <>
              <Count n={neurons} label="neuron" color={kindColor().neuron} />
              <Count n={engrams} label="engram" color={kindColor().engram} />
              <Count n={effectors} label="effector" color={kindColor().effector} />
              <Count n={receptors} label="receptor" color={kindColor().receptor} />
            </>
          )}
        </div>
        {probe.warnings.length > 0 && (
          <div style={{ fontSize: 13, color: C.textFaint, fontWeight: 600, marginTop: 9, lineHeight: 1.5 }}>
            {probe.warnings.length} thing{probe.warnings.length === 1 ? "" : "s"} to know once it's
            open — shown in the workspace.
          </div>
        )}
      </div>
    );
  }

  return (
    <div style={verdictStyle}>
      <div style={{ fontSize: 14, color: C.textDim, fontWeight: 600, lineHeight: 1.55 }}>{probe.reason}</div>
      {probe.children.length > 0 && (
        <>
          <div style={{ fontSize: 13, color: C.textFaint, fontWeight: 600, margin: "10px 0 6px", textTransform: "uppercase", letterSpacing: "0.06em" }}>
            {probe.children.length} project{probe.children.length === 1 ? "" : "s"} inside
          </div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6, maxHeight: 128, overflowY: "auto" }}>
            {probe.children.map((c) => {
              const total =
                c.counts.neurons + c.counts.engrams + c.counts.effectors + (c.counts.receptors ?? 0);
              return (
                <span
                  key={c.path}
                  onClick={() => onOpenChild(c.path, c.name)}
                  title={`${c.counts.neurons} neurons · ${c.counts.engrams} engrams · ${c.counts.effectors} effectors · ${c.counts.receptors ?? 0} receptors`}
                  style={{ ...recentChip, cursor: "pointer" }}
                >
                  {c.name}
                  <span style={{ color: C.textFaint, fontWeight: 600, marginLeft: 6 }}>{total}</span>
                </span>
              );
            })}
          </div>
        </>
      )}
    </div>
  );
}

function Count({ n, label, color }: { n: number; label: string; color: string }) {
  if (n === 0) return null;
  return (
    <span
      style={{
        fontSize: 13.5,
        fontFamily: MONO,
        color,
        border: `1px solid ${color}44`,
        background: color + "12",
        borderRadius: 999,
        padding: "2px 9px",
      }}
    >
      {n} {label}
      {n === 1 ? "" : "s"}
    </span>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div style={{ marginBottom: 16 }}>
      <label
        style={{
          display: "block",
          fontSize: 13.5,
          color: C.textFaint, fontWeight: 600,
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

const cardStyle: CSSProperties = {
  width: 540,
  maxWidth: "100%",
  background: "var(--bg-card)",
  border: "1px solid var(--border)",
  borderRadius: 16,
  padding: 28,
  boxShadow: "0 20px 60px rgba(var(--shadow-rgb), 0.4)",
};

const inputStyle: CSSProperties = {
  width: "100%",
  background: "var(--bg-elev)",
  border: "1px solid var(--border)",
  borderRadius: 8,
  color: "var(--text)",
  padding: "10px 12px",
  fontSize: 15,
  fontFamily: MONO,
  outline: "none",
};

const verdictStyle: CSSProperties = {
  border: "1px solid var(--border)",
  borderRadius: 10,
  padding: "11px 13px",
  marginBottom: 16,
  fontSize: 14.5,
  background: "rgba(var(--fg-rgb), 0.015)",
};

const gitToggleStyle: CSSProperties = {
  display: "flex",
  alignItems: "baseline",
  gap: 9,
  flexWrap: "wrap",
  margin: "2px 0 16px",
  cursor: "pointer",
};

const recentChip: CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  fontSize: 13.5,
  fontFamily: MONO,
  color: "var(--text-dim)",
  background: "rgba(var(--fg-rgb), 0.04)",
  border: "1px solid var(--border)",
  borderRadius: 999,
  padding: "4px 10px",
};

function primaryBtnStyle(enabled: boolean): CSSProperties {
  return {
    width: "100%",
    padding: "12px 16px",
    borderRadius: 10,
    border: "none",
    background: enabled ? `linear-gradient(135deg, ${C.accent}, ${C.accent2})` : C.bgElev,
    color: enabled ? C.onPrimary : C.textFaint,
    fontWeight: 600,
    fontSize: 16,
    cursor: enabled ? "pointer" : "default",
  };
}

const secondaryBtnStyle: CSSProperties = {
  background: "transparent",
  border: "1px solid rgba(var(--accent3-rgb), 0.4)",
  color: "var(--accent3)",
  borderRadius: 6,
  padding: "6px 10px",
  fontSize: 14.5,
  cursor: "pointer",
};

const errorStyle: CSSProperties = {
  background: "rgba(var(--accent3-rgb), 0.08)",
  border: "1px solid rgba(var(--accent3-rgb), 0.3)",
  color: "var(--accent3)",
  borderRadius: 8,
  padding: "10px 12px",
  fontSize: 15,
  marginBottom: 16,
};
