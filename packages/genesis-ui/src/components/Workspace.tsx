import { useCallback, useEffect, useState } from "react";
import { detect, readScaffold } from "../api";
import type {
  ComponentResult,
  ImportWarning,
  RemoveResult,
  RestoreResult,
  ScaffoldResult,
} from "../types";
import { C, MONO } from "../theme";
import { Header } from "./Header";
import type { GenesisView } from "./Header";
import { GenesisCanvas, loadLayout } from "./GenesisCanvas";
import type { CanvasNodeData } from "./CanvasNode";
import { CodeView } from "./CodeView";
import { TestView } from "./TestView";
import { ImportNotes } from "./ImportNotes";

/**
 * The shell around a scaffolded project: reads it once, then hands the same
 * ScaffoldResult to whichever view is in front. Canvas and Code are two
 * lenses on one project, so the read and the node layout live here rather
 * than in either view.
 */
export function Workspace({
  projectPath,
  onBack,
}: {
  projectPath: string;
  onBack: () => void;
}) {
  const [scaffold, setScaffold] = useState<ScaffoldResult | null>(null);
  const [nodes, setNodes] = useState<CanvasNodeData[]>([]);
  const [view, setView] = useState<GenesisView>("canvas");
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [warnings, setWarnings] = useState<ImportWarning[]>([]);

  const load = useCallback(() => {
    readScaffold(projectPath)
      .then((s) => {
        setScaffold(s);
        setNodes(loadLayout(s));
        setError(null);
      })
      .catch(() => setError("Couldn't read this project."));
    // Re-probed on every load rather than passed in once, so a note clears
    // itself as soon as the thing it warned about stops being true.
    detect(projectPath)
      .then((d) => setWarnings(d.warnings))
      .catch(() => setWarnings([]));
  }, [projectPath]);

  useEffect(load, [load]);

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 4000);
    return () => clearTimeout(t);
  }, [toast]);

  // A new module means a new node and (usually) an edited brain.py, so
  // re-read rather than patching the graph in place.
  function onAdded(result: ComponentResult) {
    setToast(
      result.wired
        ? `Created ${result.path} · ${result.note}`
        : `Created ${result.path} · not wired (${result.note}) - attach it in brain.py yourself`,
    );
    load();
  }

  // Removal is the same story backwards, and reported the same way: what
  // happened to the module, then what happened to brain.py. The unwiring is
  // the half people don't expect, so it is never left implied.
  function onRemoved(result: RemoveResult) {
    const what =
      result.mode === "archive"
        ? `Archived ${result.file} → ${result.archived_to}`
        : `Deleted ${result.file}`;
    setToast(`${what} · ${result.note}`);
    load();
  }

  function onRestored(result: RestoreResult) {
    setToast(
      result.wired
        ? `Restored ${result.file} · ${result.note}`
        : `Restored ${result.file} · not wired (${result.note}) - attach it in brain.py yourself`,
    );
    load();
  }

  return (
    <div style={{ height: "100vh", display: "flex", flexDirection: "column" }}>
      <Header
        project={scaffold?.project ?? null}
        path={scaffold?.path ?? null}
        namespace={scaffold?.namespace ?? null}
        view={view}
        onSelectView={setView}
        onReload={load}
        onBack={onBack}
      />

      <ImportNotes warnings={warnings} />

      {error && (
        <div style={{ padding: 24, color: C.accent3, fontFamily: MONO, fontSize: 15 }}>{error}</div>
      )}

      {!error && scaffold && view === "canvas" && (
        <GenesisCanvas
          scaffold={scaffold}
          nodes={nodes}
          onNodes={setNodes}
          onAdded={onAdded}
          onRemoved={onRemoved}
        />
      )}

      {!error && scaffold && view === "code" && (
        <CodeView
          scaffold={scaffold}
          onChanged={load}
          onRemoved={onRemoved}
          onRestored={onRestored}
        />
      )}

      {!error && scaffold && view === "test" && <TestView scaffold={scaffold} />}

      {toast && (
        <div
          onClick={() => setToast(null)}
          style={{
            position: "fixed",
            left: "50%",
            bottom: 24,
            transform: "translateX(-50%)",
            zIndex: 20,
            background: "var(--bg-overlay)",
            border: `1px solid ${C.borderStrong}`,
            borderRadius: 10,
            padding: "10px 16px",
            fontFamily: MONO,
            fontSize: 14.5,
            color: C.text,
            cursor: "pointer",
            boxShadow: "0 16px 40px rgba(var(--shadow-rgb), 0.5)",
          }}
        >
          {toast}
        </div>
      )}
    </div>
  );
}
