import { useCallback, useEffect, useMemo, useState } from "react";
import type { CSSProperties, ReactNode } from "react";
import { readArchived, readFile } from "../api";
import type {
  ArchivedEntry,
  InitError,
  RemoveResult,
  RestoreResult,
  ScaffoldResult,
} from "../types";
import { C, MONO } from "../theme";
import { kindColor } from "./CanvasNode";
import type { NodeKind } from "./CanvasNode";
import { CodeEditor } from "./CodeEditor";
import { ComponentEditor } from "./ComponentEditor";
import { HelpersEditor } from "./HelpersEditor";
import { RemoveComponent, RestoreComponent } from "./RemoveComponent";

const HELPERS = "helpers.py";

interface Item {
  /** Project-relative path - what the API wants. */
  file: string;
  label: string;
  kind: NodeKind | "wiring";
}

interface Group {
  title: string;
  color: string;
  items: Item[];
}

/**
 * Group the scaffold into the Code tab's sidebar: the four primitives first
 * (that's what you came to edit), then the wiring and docs that hold them
 * together. helpers.py is pulled out above all of it - it isn't a component,
 * and it's the one file everything else can reach.
 */
function groupsOf(scaffold: ScaffoldResult): Group[] {
  const inPkg = (pkg: string) =>
    scaffold.files.filter((f) => f.startsWith(pkg + "/") && !f.endsWith("__init__.py"));

  const byFile = (nodes: { id: string; file: string }[], pkg: string, kind: NodeKind): Item[] =>
    inPkg(pkg).map((f) => {
      const node = nodes.find((n) => `${pkg}/${n.file}` === f);
      return { file: f, label: node?.id ?? f.split("/")[1], kind };
    });

  const wiring: Item[] = scaffold.files
    .filter((f) => !f.includes("/") && f !== HELPERS)
    .map((f) => ({ file: f, label: f, kind: "wiring" as const }));

  return [
    { title: "Neurons · think", color: kindColor().neuron, items: byFile(scaffold.neurons, "neurons", "neuron") },
    { title: "Engrams · remember", color: kindColor().engram, items: byFile(scaffold.engrams, "engram", "engram") },
    { title: "Effectors · act", color: kindColor().effector, items: byFile(scaffold.effectors, "effector", "effector") },
    { title: "Receptors · listen", color: kindColor().receptor, items: byFile(scaffold.receptors ?? [], "receptors", "receptor") },
    { title: "Wiring", color: C.textFaint, fontWeight: 600, items: wiring },
  ].filter((g) => g.items.length > 0);
}

/**
 * The Code tab.
 *
 * Two ways of working, because there are two kinds of file. A component is a
 * protocol surface - an identity plus a set of decorators - so it gets a
 * config form and one code box per behaviour. helpers.py is ordinary Python
 * every component can import, so it gets an ordinary editor. The wiring
 * files are read-only here; brain.py in particular is maintained for you
 * when components are added - and when they're taken away.
 *
 * Removal lives in the sidebar rather than in the editor pane on purpose: the
 * sidebar is the list of what this project has, so adding to it and taking
 * away from it belong in the same place - and a module too broken to parse
 * still has a row here, which is exactly when you most want to archive it.
 */
export function CodeView({
  scaffold,
  onChanged,
  onRemoved,
  onRestored,
}: {
  scaffold: ScaffoldResult;
  onChanged: () => void;
  onRemoved: (result: RemoveResult) => void;
  onRestored: (result: RestoreResult) => void;
}) {
  const groups = useMemo(() => groupsOf(scaffold), [scaffold]);
  const hasHelpers = scaffold.files.includes(HELPERS);
  const [file, setFile] = useState<string>(HELPERS);
  const [menuFor, setMenuFor] = useState<string | null>(null);
  const [archived, setArchived] = useState<ArchivedEntry[]>([]);
  const [openArchive, setOpenArchive] = useState(false);

  // The archive is read separately from the scaffold because it deliberately
  // isn't part of it - _archive is in the backend's skip list, so nothing in
  // there reaches this component any other way.
  const loadArchived = useCallback(() => {
    readArchived(scaffold.path)
      .then((r) => setArchived(r.entries))
      .catch(() => setArchived([]));
  }, [scaffold.path]);

  useEffect(loadArchived, [loadArchived, scaffold]);

  // Reselect when the project changes underneath us (reload, new component,
  // or the one that was open being archived).
  useEffect(() => {
    if (file !== HELPERS && !scaffold.files.includes(file)) setFile(HELPERS);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scaffold]);

  const isComponent = /^(neurons|effector|engram|receptors)\//.test(file);

  function afterRemove(r: RemoveResult) {
    setMenuFor(null);
    loadArchived();
    onRemoved(r);
  }

  return (
    <div style={{ flex: 1, display: "flex", minHeight: 0 }}>
      <div style={sidebarStyle}>
        {/* Anywhere-else click closes an open menu. A backdrop rather than a
            document listener so it can't outlive this view. */}
        {menuFor && (
          <div
            onClick={() => setMenuFor(null)}
            style={{ position: "fixed", inset: 0, zIndex: 5 }}
          />
        )}

        <div style={{ padding: "0 0 12px" }}>
          <div style={{ ...sectionStyle, color: C.accent2 }}>Shared</div>
          <SidebarItem
            label="helpers"
            sub={hasHelpers ? HELPERS : "not created yet"}
            color={C.accent2}
            on={file === HELPERS}
            onClick={() => setFile(HELPERS)}
          />
        </div>

        {groups.map((g) => (
          <div key={g.title} style={{ marginBottom: 16 }}>
            <div style={{ ...sectionStyle, color: g.color }}>{g.title}</div>
            {g.items.map((item) => (
              <SidebarItem
                key={item.file}
                label={item.label}
                sub={item.kind === "wiring" ? undefined : item.file}
                color={g.color}
                on={item.file === file}
                onClick={() => setFile(item.file)}
                // Wiring files are the project's spine - brain.py is where
                // everything else is unwired *to*, so it is not itself a
                // thing this menu can take away.
                onMenu={item.kind === "wiring" ? undefined : () =>
                  setMenuFor((m) => (m === item.file ? null : item.file))
                }
                menu={
                  menuFor === item.file && (
                    <RemoveComponent
                      projectPath={scaffold.path}
                      file={item.file}
                      label={item.label}
                      accent={g.color}
                      layout="menu"
                      onRemoved={afterRemove}
                    />
                  )
                }
              />
            ))}
          </div>
        ))}

        {archived.length > 0 && (
          <div style={{ marginBottom: 16 }}>
            <div
              onClick={() => setOpenArchive((v) => !v)}
              style={{
                ...sectionStyle,
                color: C.textFaint,
                cursor: "pointer",
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
              }}
            >
              <span>Archived · {archived.length}</span>
              <span style={{ fontFamily: MONO, fontSize: 14 }}>
                {openArchive ? "−" : "+"}
              </span>
            </div>
            {openArchive &&
              archived.map((e) => (
                <div key={e.file} style={archivedRow}>
                  <div
                    style={{
                      fontFamily: MONO,
                      fontSize: 14,
                      color: C.textDim,
                      fontWeight: 600,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                    title={e.origin}
                  >
                    <span style={{ color: e.kind ? kindColor()[e.kind] : C.textFaint }}>▪ </span>
                    {e.id}
                  </div>
                  <div style={{ fontSize: 12.5, color: C.textFaint, fontWeight: 600, margin: "1px 0 7px" }}>
                    {e.origin}
                  </div>
                  <RestoreComponent
                    projectPath={scaffold.path}
                    file={e.file}
                    origin={e.origin}
                    restorable={e.restorable}
                    onRestored={(r) => {
                      loadArchived();
                      onRestored(r);
                    }}
                    onRemoved={afterRemove}
                  />
                </div>
              ))}
          </div>
        )}
      </div>

      {file === HELPERS ? (
        <HelpersEditor projectPath={scaffold.path} exists={hasHelpers} onCreated={onChanged} />
      ) : isComponent ? (
        <ComponentEditor
          key={file}
          projectPath={scaffold.path}
          file={file}
          onChanged={onChanged}
        />
      ) : (
        <ReadOnlyFile projectPath={scaffold.path} file={file} />
      )}
    </div>
  );
}

function SidebarItem({
  label,
  sub,
  color,
  on,
  onClick,
  onMenu,
  menu,
}: {
  label: string;
  sub?: string;
  color: string;
  on: boolean;
  onClick: () => void;
  /** Omitted for rows that have nothing to remove. */
  onMenu?: () => void;
  menu?: ReactNode;
}) {
  const [hover, setHover] = useState(false);
  const open = Boolean(menu);

  return (
    <div
      style={{ position: "relative" }}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
    >
      <div
        onClick={onClick}
        title={sub}
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          padding: "6px 8px 6px 14px",
          cursor: "pointer",
          borderLeft: `2px solid ${on ? color : "transparent"}`,
          background: on || open ? "rgba(var(--fg-rgb), 0.045)" : "transparent",
          color: on ? C.text : C.textDim,
          fontFamily: MONO,
          fontSize: 14.5,
        }}
      >
        <div style={{ minWidth: 0, flex: 1, overflow: "hidden" }}>
          <div style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {label}
          </div>
          {sub && (
            <div
              style={{
                fontSize: 13,
                color: C.textFaint,
                fontWeight: 600,
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              }}
            >
              {sub}
            </div>
          )}
        </div>
        {onMenu && (
          <button
            onClick={(e) => {
              e.stopPropagation();
              onMenu();
            }}
            title="Archive or delete this component"
            aria-label={`Archive or delete ${label}`}
            style={{
              ...menuButton,
              // Kept in the layout at all times so revealing it can't nudge
              // the label; only its ink comes and goes.
              opacity: hover || open ? 1 : 0,
              color: open ? C.text : C.textDim,
            }}
          >
            ⋯
          </button>
        )}
      </div>
      {open && <div style={menuPopover}>{menu}</div>}
    </div>
  );
}

/** brain.py, config.py, README - shown, not edited here. */
function ReadOnlyFile({ projectPath, file }: { projectPath: string; file: string }) {
  const [text, setText] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setText(null);
    setError(null);
    readFile(projectPath, file)
      .then((r) => !cancelled && setText(r.text))
      .catch((e) => !cancelled && setError((e as InitError).error || "Couldn't read that file."));
    return () => {
      cancelled = true;
    };
  }, [projectPath, file]);

  return (
    <div style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0 }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "9px 16px",
          borderBottom: `1px solid ${C.border}`,
          fontFamily: MONO,
          fontSize: 13.5,
          color: C.textDim, fontWeight: 600,
        }}
      >
        <span>{file}</span>
        <span style={{ color: C.textFaint, fontWeight: 600, }}>
          {file === "brain.py"
            ? "read-only · Genesis maintains this as components come and go"
            : "read-only"}
        </span>
      </div>
      <div style={{ flex: 1, overflow: "auto", minHeight: 0, padding: 14 }}>
        {error && <div style={{ color: C.accent3, fontSize: 15 }}>{error}</div>}
        {!error && text === null && <div style={{ color: C.textFaint, fontWeight: 600, fontSize: 15 }}>Reading…</div>}
        {!error && text !== null && (
          <CodeEditor value={text} onChange={() => {}} readOnly minRows={1} maxRows={4000} />
        )}
      </div>
    </div>
  );
}

const sidebarStyle: CSSProperties = {
  width: 250,
  flexShrink: 0,
  borderRight: "1px solid var(--border)",
  background: "var(--bg-elev)",
  overflowY: "auto",
  padding: "12px 0",
};

const sectionStyle: CSSProperties = {
  padding: "0 14px 6px",
  fontSize: 13,
  letterSpacing: "0.08em",
  textTransform: "uppercase",
  opacity: 0.85,
};

const menuButton: CSSProperties = {
  flexShrink: 0,
  width: 22,
  height: 22,
  lineHeight: "18px",
  padding: 0,
  borderRadius: 6,
  border: "1px solid transparent",
  background: "transparent",
  fontFamily: MONO,
  fontSize: 15,
  cursor: "pointer",
  transition: "opacity 0.12s",
};

const menuPopover: CSSProperties = {
  position: "absolute",
  left: 12,
  right: 8,
  top: "100%",
  zIndex: 6,
  marginTop: 2,
  padding: 10,
  borderRadius: 10,
  background: "var(--bg-panel)",
  WebkitBackdropFilter: "blur(20px)",
  backdropFilter: "blur(20px)",
  border: "1px solid var(--border-strong)",
  boxShadow: "0 18px 50px rgba(var(--shadow-rgb), 0.45)",
};

const archivedRow: CSSProperties = {
  padding: "7px 14px 11px",
  borderLeft: "2px solid transparent",
};
