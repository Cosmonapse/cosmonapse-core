import { useEffect, useState } from "react";
import type { CSSProperties } from "react";
import { browse } from "../api";
import type { BrowseEntry } from "../types";
import { C } from "../theme";

/**
 * Server-backed directory browser: the browser can't open a native OS folder
 * dialog, but cosmo genesis's local server can see the real filesystem, so
 * this walks it a directory at a time via GET /api/browse.
 */
export function FolderBrowser({
  path,
  onChange,
}: {
  path: string;
  onChange: (path: string) => void;
}) {
  const [result, setResult] = useState<{ path: string; parent: string | null; entries: BrowseEntry[] } | null>(
    null,
  );
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    browse(path || undefined)
      .then((r) => {
        if (cancelled) return;
        setResult(r);
        setError(null);
        if (r.path !== path) onChange(r.path);
      })
      .catch(() => {
        if (!cancelled) setError("Couldn't read that folder.");
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path]);

  return (
    <div
      style={{
        border: `1px solid ${C.border}`,
        borderRadius: 10,
        background: C.bgElev,
        overflow: "hidden",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          padding: "8px 12px",
          borderBottom: `1px solid ${C.border}`,
        }}
      >
        <button
          disabled={!result?.parent}
          onClick={() => result?.parent && onChange(result.parent)}
          style={navBtnStyle(!!result?.parent)}
          title="Up one level"
        >
          &uarr;
        </button>
        <span
          style={{
            fontFamily: "ui-monospace,Menlo,monospace",
            fontSize: 12,
            color: C.textDim,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          {result?.path ?? path}
        </span>
      </div>
      <div style={{ maxHeight: 220, overflowY: "auto" }}>
        {error && <div style={{ padding: 12, color: C.accent3, fontSize: 13 }}>{error}</div>}
        {!error && result && result.entries.length === 0 && (
          <div style={{ padding: 12, color: C.textFaint, fontSize: 13 }}>No subfolders here.</div>
        )}
        {!error &&
          result?.entries.map((e) => (
            <div
              key={e.path}
              onClick={() => onChange(e.path)}
              style={{
                padding: "7px 12px",
                fontSize: 13,
                cursor: "pointer",
                color: C.text,
                display: "flex",
                alignItems: "center",
                gap: 8,
              }}
              onMouseEnter={(ev) => (ev.currentTarget.style.background = "rgba(255,255,255,0.04)")}
              onMouseLeave={(ev) => (ev.currentTarget.style.background = "transparent")}
            >
              <span style={{ color: C.textFaint }}>&#128193;</span>
              {e.name}
            </div>
          ))}
      </div>
    </div>
  );
}

function navBtnStyle(enabled: boolean): CSSProperties {
  return {
    background: "transparent",
    border: `1px solid ${C.border}`,
    borderRadius: 6,
    color: enabled ? C.text : C.textFaint,
    cursor: enabled ? "pointer" : "default",
    width: 24,
    height: 24,
    fontSize: 13,
    lineHeight: 1,
  };
}
