import { useCallback, useEffect, useMemo, useState } from "react";
import type { CSSProperties, ReactNode } from "react";
import { readArchived } from "../api";
import type {
  ArchivedEntry,
  RemoveResult,
  RestoreResult,
  ScaffoldResult,
} from "../types";
import { C, MONO } from "../theme";
import { kindColor } from "./CanvasNode";
import type { NodeKind } from "./CanvasNode";
import { ComponentEditor } from "./ComponentEditor";
import { FileEditor } from "./FileEditor";
import { HelpersEditor } from "./HelpersEditor";
import { RemoveComponent, RestoreComponent } from "./RemoveComponent";

const HELPERS = "helpers.py";

interface Item {
  /** Project-relative path - what the API wants. */
  file: string;
  label: string;
  kind: NodeKind | "wiring";
  /** A package's __init__.py: editable, but not a component to remove. */
  init?: boolean;
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
  const inPkg = (pkg: string) => scaffold.files.filter((f) => f.startsWith(pkg + "/"));

  const byFile = (nodes: { id: string; file: string }[], pkg: string, kind: NodeKind): Item[] =>
    inPkg(pkg).map((f) => {
      if (f === `${pkg}/__init__.py`) return { file: f, label: "__init__.py", kind, init: true };
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
 * every component can import, so it gets an ordinary editor. Every other
 * file is editable as plain text too: the wiring files, each package's
 * __init__.py, and a component's own source through its Source view, for
 * whatever the form doesn't model. brain.py is still maintained for you when
 * components are added and taken away; hand edits to it are kept, and the
 * next add or remove works from whatever is on disk.
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
  // How a component module is shown. Kept across files, so someone reading
  // raw source stays in Source as they move down the sidebar.
  const [componentView, setComponentView] = useState<"form" | "source">("form");

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

  const isComponent =
    /^(neurons|effector|engram|receptors)\//.test(file) && !file.endsWith("__init__.py");

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
                // thing this menu can take away. Nor is a package's
                // __init__.py, which is not a component.
                onMenu={item.kind === "wiring" || item.init ? undefined : () =>
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
        <div style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0, minHeight: 0 }}>
          <div style={viewTabs}>
            {(["form", "source"] as const).map((v) => (
              <button
                key={v}
                onClick={() => setComponentView(v)}
                style={viewTab(componentView === v)}
              >
                {v === "form" ? "Form" : "Source"}
              </button>
            ))}
          </div>
          {componentView === "form" ? (
            // Keyed on the file only: switching back from Source remounts it,
            // which re-reads the module, so a raw edit shows up in the form.
            <ComponentEditor
              key={file}
              projectPath={scaffold.path}
              file={file}
              onChanged={onChanged}
            />
          ) : (
            <FileEditor
              key={file}
              projectPath={scaffold.path}
              file={file}
              note="the whole module, including what the form doesn't model"
              onSaved={onChanged}
            />
          )}
        </div>
      ) : (
        <FileEditor
          key={file}
          projectPath={scaffold.path}
          file={file}
          note={
            file === "brain.py"
              ? "Genesis also edits this as components come and go · your edits are kept"
              : undefined
          }
          onSaved={onChanged}
        />
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

const viewTabs: CSSProperties = {
  display: "flex",
  gap: 4,
  padding: "8px 16px 0",
  borderBottom: "1px solid var(--border)",
};

function viewTab(on: boolean): CSSProperties {
  return {
    background: "transparent",
    border: "none",
    borderBottom: `2px solid ${on ? "var(--accent2)" : "transparent"}`,
    color: on ? "var(--text)" : "var(--text-dim)",
    padding: "5px 10px 7px",
    fontFamily: MONO,
    fontSize: 13.5,
    fontWeight: 600,
    cursor: "pointer",
  };
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
