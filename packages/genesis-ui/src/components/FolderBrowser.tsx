import { useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties } from "react";
import { browse } from "../api";
import type { BrowseEntry } from "../types";
import { C, MONO } from "../theme";

const RECENTS_KEY = "genesis:recent-folders";
const MAX_RECENTS = 5;

function loadRecents(): string[] {
  try {
    const raw = localStorage.getItem(RECENTS_KEY);
    return raw ? (JSON.parse(raw) as string[]) : [];
  } catch {
    return [];
  }
}

export function pushRecent(path: string) {
  try {
    const next = [path, ...loadRecents().filter((p) => p !== path)].slice(0, MAX_RECENTS);
    localStorage.setItem(RECENTS_KEY, JSON.stringify(next));
  } catch {
    // best-effort
  }
}

/**
 * Split a path into clickable breadcrumb segments.
 *
 * The server hands back whatever the host OS uses, so this sniffs the
 * separator rather than assuming POSIX - Genesis runs on Windows too, where
 * paths come back as C:\Users\you\projects.
 */
function crumbs(path: string): { label: string; path: string }[] {
  if (!path) return [];
  const sep = path.includes("\\") && !path.startsWith("/") ? "\\" : "/";
  const parts = path.split(sep).filter(Boolean);
  const out: { label: string; path: string }[] = [];
  if (sep === "/") out.push({ label: "/", path: "/" });
  let acc = sep === "/" ? "" : "";
  parts.forEach((part) => {
    acc = acc ? acc + sep + part : sep === "/" ? "/" + part : part;
    out.push({ label: part, path: acc });
  });
  return out;
}

/**
 * Server-backed directory browser: the browser can't open a native OS folder
 * dialog, but cosmo genesis's local server can see the real filesystem, so
 * this walks it a directory at a time via GET /api/browse.
 *
 * Built to be driven from the keyboard - the filter box owns focus, so
 * type-to-narrow, arrow-to-move and Enter-to-open work without ever
 * reaching for the mouse. Backspace on an empty filter goes up a level.
 */
export function FolderBrowser({
  path,
  onChange,
}: {
  path: string;
  onChange: (path: string) => void;
}) {
  const [result, setResult] = useState<{
    path: string;
    parent: string | null;
    entries: BrowseEntry[];
  } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [filter, setFilter] = useState("");
  const [active, setActive] = useState(0);
  const [recents, setRecents] = useState<string[]>(loadRecents);
  const listRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    browse(path || undefined)
      .then((r) => {
        if (cancelled) return;
        setResult(r);
        setError(null);
        setFilter("");
        setActive(0);
        if (r.path !== path) onChange(r.path);
      })
      .catch(() => {
        if (!cancelled) setError("Couldn't read that folder.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path]);

  const entries = useMemo(() => {
    const q = filter.trim().toLowerCase();
    const all = result?.entries ?? [];
    if (!q) return all;
    return all.filter((e) => e.name.toLowerCase().includes(q));
  }, [result, filter]);

  // Keep the highlighted row in view as the arrows walk past the fold.
  useEffect(() => {
    const el = listRef.current?.querySelector<HTMLElement>(`[data-idx="${active}"]`);
    el?.scrollIntoView({ block: "nearest" });
  }, [active]);

  function open(next: string) {
    onChange(next);
    inputRef.current?.focus();
  }

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((i) => Math.min(i + 1, Math.max(entries.length - 1, 0)));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((i) => Math.max(i - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      const entry = entries[active];
      if (entry) open(entry.path);
    } else if (e.key === "Backspace" && !filter && result?.parent) {
      e.preventDefault();
      open(result.parent);
    } else if (e.key === "Escape" && filter) {
      e.preventDefault();
      setFilter("");
    }
  }

  const trail = crumbs(result?.path ?? path);

  return (
    <div
      style={{
        border: `1px solid ${C.border}`,
        borderRadius: 10,
        background: C.bgElev,
        overflow: "hidden",
      }}
    >
      {/* Breadcrumb trail - every ancestor is one click away */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          padding: "8px 10px",
          borderBottom: `1px solid ${C.border}`,
          overflowX: "auto",
          whiteSpace: "nowrap",
        }}
      >
        <button onClick={() => open("")} style={iconBtn(true)} title="Home folder">
          ⌂
        </button>
        <button
          disabled={!result?.parent}
          onClick={() => result?.parent && open(result.parent)}
          style={iconBtn(!!result?.parent)}
          title="Up one level (Backspace)"
        >
          ↑
        </button>
        <span style={{ color: C.textFaint, fontWeight: 600, margin: "0 2px" }}>│</span>
        {trail.map((crumb, i) => (
          <span key={crumb.path} style={{ display: "flex", alignItems: "center", gap: 6 }}>
            {i > 0 && <span style={{ color: C.textFaint, fontWeight: 600, fontSize: 13.5 }}>›</span>}
            <span
              onClick={() => open(crumb.path)}
              style={{
                fontFamily: MONO,
                fontSize: 13.5,
                cursor: "pointer",
                color: i === trail.length - 1 ? C.accent2 : C.textDim,
                padding: "2px 5px",
                borderRadius: 5,
                background: i === trail.length - 1 ? "rgba(var(--accent2-rgb), 0.1)" : "transparent",
              }}
            >
              {crumb.label}
            </span>
          </span>
        ))}
      </div>

      {/* Typeahead - also the keyboard-nav surface for the list below */}
      <div style={{ padding: "8px 10px", borderBottom: `1px solid ${C.border}` }}>
        <input
          ref={inputRef}
          value={filter}
          onChange={(e) => {
            setFilter(e.target.value);
            setActive(0);
          }}
          onKeyDown={onKeyDown}
          placeholder="Filter folders…  ↑↓ move · Enter open · ⌫ up"
          style={{
            width: "100%",
            background: "transparent",
            border: "none",
            outline: "none",
            color: C.text,
            fontSize: 14.5,
            fontFamily: MONO,
          }}
        />
      </div>

      <div ref={listRef} style={{ height: 260, overflowY: "auto" }}>
        {error && <div style={emptyStyle(C.accent3)}>{error}</div>}
        {!error && loading && !result && <div style={emptyStyle(C.textFaint)}>Reading…</div>}
        {!error && result && entries.length === 0 && (
          <div style={emptyStyle(C.textFaint)}>
            {filter ? `No folder matching "${filter}".` : "No subfolders here."}
          </div>
        )}
        {!error &&
          entries.map((e, i) => {
            const on = i === active;
            return (
              <div
                key={e.path}
                data-idx={i}
                onMouseEnter={() => setActive(i)}
                onClick={() => open(e.path)}
                style={{
                  padding: "7px 12px",
                  fontSize: 15,
                  cursor: "pointer",
                  color: on ? C.text : C.textDim,
                  background: on ? "rgba(var(--accent2-rgb), 0.08)" : "transparent",
                  borderLeft: `2px solid ${on ? C.accent2 : "transparent"}`,
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                }}
              >
                <span style={{ color: on ? C.accent2 : C.textFaint }}>&#128193;</span>
                <Highlighted text={e.name} query={filter} />
              </div>
            );
          })}
      </div>

      {/* Where you'll actually land, plus the last few places you picked */}
      <div
        style={{
          borderTop: `1px solid ${C.border}`,
          padding: "8px 10px",
          display: "flex",
          flexDirection: "column",
          gap: 8,
        }}
      >
        <div
          style={{
            fontFamily: MONO,
            fontSize: 13.5,
            color: C.textDim, fontWeight: 600,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
          title={result?.path ?? path}
        >
          <span style={{ color: C.textFaint, fontWeight: 600, }}>selected · </span>
          {result?.path ?? path}
        </div>
        {recents.length > 0 && (
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
            {recents.map((r) => (
              <span
                key={r}
                onClick={() => open(r)}
                title={r}
                style={{
                  fontFamily: MONO,
                  fontSize: 13,
                  color: C.textDim, fontWeight: 600,
                  border: `1px solid ${C.border}`,
                  borderRadius: 999,
                  padding: "3px 9px",
                  cursor: "pointer",
                  maxWidth: 140,
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                }}
              >
                {r.split(/[\\/]/).filter(Boolean).pop() ?? r}
              </span>
            ))}
            <span
              onClick={() => {
                localStorage.removeItem(RECENTS_KEY);
                setRecents([]);
              }}
              style={{ fontFamily: MONO, fontSize: 13, color: C.textFaint, fontWeight: 600, cursor: "pointer", padding: "3px 4px" }}
            >
              clear
            </span>
          </div>
        )}
      </div>
    </div>
  );
}

/** Bold the part of a folder name the filter matched. */
function Highlighted({ text, query }: { text: string; query: string }) {
  const q = query.trim();
  if (!q) return <>{text}</>;
  const at = text.toLowerCase().indexOf(q.toLowerCase());
  if (at < 0) return <>{text}</>;
  return (
    <>
      {text.slice(0, at)}
      <span style={{ color: C.accent2 }}>{text.slice(at, at + q.length)}</span>
      {text.slice(at + q.length)}
    </>
  );
}

function iconBtn(enabled: boolean): CSSProperties {
  return {
    background: "transparent",
    border: `1px solid ${C.border}`,
    borderRadius: 6,
    color: enabled ? C.text : C.textFaint,
    cursor: enabled ? "pointer" : "default",
    width: 24,
    height: 24,
    fontSize: 15,
    lineHeight: 1,
    flexShrink: 0,
  };
}

function emptyStyle(color: string): CSSProperties {
  return { padding: 14, color, fontSize: 15 };
}
