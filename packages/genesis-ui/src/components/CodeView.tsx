import { useEffect, useMemo, useState } from "react";
import type { CSSProperties } from "react";
import { readFile } from "../api";
import type { InitError, ScaffoldResult } from "../types";
import { C, MONO } from "../theme";
import { kindColor } from "./CanvasNode";
import type { NodeKind } from "./CanvasNode";
import { CodeEditor } from "./CodeEditor";
import { ComponentEditor } from "./ComponentEditor";
import { HelpersEditor } from "./HelpersEditor";

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
 * when components are added.
 */
export function CodeView({
  scaffold,
  onChanged,
}: {
  scaffold: ScaffoldResult;
  onChanged: () => void;
}) {
  const groups = useMemo(() => groupsOf(scaffold), [scaffold]);
  const hasHelpers = scaffold.files.includes(HELPERS);
  const [file, setFile] = useState<string>(HELPERS);

  // Reselect when the project changes underneath us (reload, new component).
  useEffect(() => {
    if (file !== HELPERS && !scaffold.files.includes(file)) setFile(HELPERS);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scaffold]);

  const isComponent = /^(neurons|effector|engram|receptors)\//.test(file);

  return (
    <div style={{ flex: 1, display: "flex", minHeight: 0 }}>
      <div style={sidebarStyle}>
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
              />
            ))}
          </div>
        ))}
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
}: {
  label: string;
  sub?: string;
  color: string;
  on: boolean;
  onClick: () => void;
}) {
  return (
    <div
      onClick={onClick}
      title={sub}
      style={{
        padding: "6px 14px",
        cursor: "pointer",
        borderLeft: `2px solid ${on ? color : "transparent"}`,
        background: on ? "rgba(var(--fg-rgb), 0.045)" : "transparent",
        color: on ? C.text : C.textDim,
        fontFamily: MONO,
        fontSize: 14.5,
        overflow: "hidden",
        textOverflow: "ellipsis",
        whiteSpace: "nowrap",
      }}
    >
      {label}
      {sub && <div style={{ fontSize: 13, color: C.textFaint, fontWeight: 600, }}>{sub}</div>}
    </div>
  );
}

/** brain.py, demo.py, config.py, README - shown, not edited here. */
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
            ? "read-only · Genesis maintains this when you add components"
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
