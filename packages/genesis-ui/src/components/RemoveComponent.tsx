import { useState } from "react";
import type { CSSProperties } from "react";
import { removeComponent, restoreComponent } from "../api";
import { C, MONO } from "../theme";
import type { InitError, RemoveMode, RemoveResult, RestoreResult } from "../types";

/**
 * What each mode costs, said before it happens rather than after.
 *
 * Both edit brain.py, and that is the part worth spelling out: the surprise
 * in removing a component isn't the missing file, it's the wiring that went
 * with it. Archive names the folder so "where did it go" has an answer on
 * screen; delete says the word that has no undo.
 */
const CONFIRM: Record<RemoveMode, { verb: string; blurb: string; danger: boolean }> = {
  archive: {
    verb: "Archive",
    blurb:
      "Moves the module to _archive/ and takes its import, its attach line and its " +
      "builder out of brain.py. Restore puts both back.",
    danger: false,
  },
  delete: {
    verb: "Delete",
    blurb:
      "Deletes the module from disk and takes its import, its attach line and its " +
      "builder out of brain.py. There is no undo.",
    danger: true,
  },
};

/**
 * Archive / Delete for one component, with the confirm step in between.
 *
 * Shared by the canvas (in the selected-node panel) and the Code tab (in the
 * sidebar's per-item menu) so the two can't end up asking differently for the
 * same irreversible thing. `layout` only moves the buttons around; every
 * variant asks the same question and calls the same endpoint.
 */
export function RemoveComponent({
  projectPath,
  file,
  label,
  accent,
  layout = "row",
  onRemoved,
}: {
  projectPath: string;
  /** Project-relative, e.g. "neurons/hello.py". */
  file: string;
  /** What to call it in the confirm sentence - the component's id. */
  label: string;
  accent: string;
  layout?: "row" | "menu";
  onRemoved: (result: RemoveResult) => void;
}) {
  const [mode, setMode] = useState<RemoveMode | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function go(m: RemoveMode) {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      onRemoved(await removeComponent({ path: projectPath, file, mode: m }));
      setMode(null);
    } catch (e) {
      setError((e as InitError).error || `Couldn't ${m} that component.`);
    } finally {
      setBusy(false);
    }
  }

  if (mode) {
    const c = CONFIRM[mode];
    return (
      <div style={{ marginTop: layout === "menu" ? 0 : 10 }}>
        <div style={confirmLine}>
          {c.verb} <span style={{ color: C.text }}>{label}</span>?
        </div>
        <div style={blurbStyle}>{c.blurb}</div>
        {error && <div style={errorStyle}>{error}</div>}
        <div style={{ display: "flex", gap: 6, marginTop: 8 }}>
          <button
            onClick={() => go(mode)}
            disabled={busy}
            style={{
              ...actionStyle,
              flex: 1,
              color: c.danger ? C.onPrimary : accent,
              background: c.danger ? C.accent3 : accent + "1c",
              borderColor: c.danger ? C.accent3 : accent + "55",
            }}
          >
            {busy ? `${c.verb.replace(/e$/, "")}ing…` : c.verb}
          </button>
          <button onClick={() => setMode(null)} disabled={busy} style={actionStyle}>
            Cancel
          </button>
        </div>
      </div>
    );
  }

  return (
    <div
      style={{
        display: "flex",
        gap: 6,
        marginTop: layout === "menu" ? 0 : 10,
        flexDirection: layout === "menu" ? "column" : "row",
      }}
    >
      <button onClick={() => setMode("archive")} style={{ ...actionStyle, flex: 1 }}>
        Archive
      </button>
      <button
        onClick={() => setMode("delete")}
        style={{ ...actionStyle, flex: 1, color: C.accent3, borderColor: C.accent3 + "44" }}
      >
        Delete
      </button>
    </div>
  );
}

/**
 * The other direction: put an archived module back and re-wire it.
 *
 * Refuses ahead of time when something already occupies the path it came
 * from, because the honest answer there is "rename the one you have", not a
 * 409 after a click.
 */
export function RestoreComponent({
  projectPath,
  file,
  origin,
  restorable,
  onRestored,
  onRemoved,
}: {
  projectPath: string;
  /** The archived path: "_archive/neurons/hello.py". */
  file: string;
  origin: string;
  restorable: boolean;
  onRestored: (result: RestoreResult) => void;
  onRemoved: (result: RemoveResult) => void;
}) {
  const [busy, setBusy] = useState<"restore" | "delete" | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function run<T>(what: "restore" | "delete", call: () => Promise<T>, done: (r: T) => void) {
    if (busy) return;
    setBusy(what);
    setError(null);
    try {
      done(await call());
    } catch (e) {
      setError((e as InitError).error || `Couldn't ${what} that component.`);
    } finally {
      setBusy(null);
    }
  }

  return (
    <div>
      <div style={{ display: "flex", gap: 6 }}>
        <button
          onClick={() =>
            run("restore", () => restoreComponent(projectPath, file), onRestored)
          }
          disabled={!restorable || busy !== null}
          title={
            restorable
              ? `Moves it back to ${origin} and wires it into brain.py.`
              : `${origin} already exists - rename or remove it first.`
          }
          style={{
            ...actionStyle,
            flex: 1,
            opacity: restorable ? 1 : 0.45,
            cursor: restorable ? "pointer" : "default",
          }}
        >
          {busy === "restore" ? "Restoring…" : "Restore"}
        </button>
        <button
          onClick={() =>
            confirming
              ? run(
                  "delete",
                  () => removeComponent({ path: projectPath, file, mode: "delete" }),
                  onRemoved,
                )
              : setConfirming(true)
          }
          disabled={busy !== null}
          style={{
            ...actionStyle,
            flex: 1,
            color: confirming ? C.onPrimary : C.accent3,
            background: confirming ? C.accent3 : "transparent",
            borderColor: C.accent3 + (confirming ? "" : "44"),
          }}
        >
          {busy === "delete" ? "Deleting…" : confirming ? "For good?" : "Delete"}
        </button>
      </div>
      {error && <div style={errorStyle}>{error}</div>}
    </div>
  );
}

const actionStyle: CSSProperties = {
  padding: "6px 10px",
  borderRadius: 8,
  border: "1px solid var(--border)",
  background: "transparent",
  color: "var(--text-dim)",
  fontFamily: MONO,
  fontSize: 13.5,
  fontWeight: 600,
  cursor: "pointer",
  whiteSpace: "nowrap",
};

const confirmLine: CSSProperties = {
  fontFamily: MONO,
  fontSize: 13.5,
  color: "var(--text-dim)",
  fontWeight: 600,
};

const blurbStyle: CSSProperties = {
  fontSize: 13,
  color: "var(--text-faint)",
  fontWeight: 600,
  lineHeight: 1.45,
  marginTop: 5,
};

const errorStyle: CSSProperties = {
  fontSize: 13,
  color: "var(--accent3)",
  lineHeight: 1.4,
  marginTop: 7,
};
