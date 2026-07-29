import { useEffect, useRef, useState } from "react";
import type { CSSProperties, ReactNode } from "react";
import { C, MONO } from "../theme";
import { ThemeToggle } from "./ThemeToggle";

/**
 * Everything about the *session* rather than the brain, behind one gear.
 *
 * Reload, new brain and the theme switch were three loose buttons competing
 * with the view switcher and the synapse indicator for the same strip of
 * header. None of them is something you reach for while working, so they sit
 * in a menu and give the space back to the two things that are: what you're
 * looking at, and whether it's live.
 */
export function SettingsMenu({
  onReload,
  onBack,
}: {
  onReload: () => void;
  onBack: () => void;
}) {
  const [open, setOpen] = useState(false);
  const wrap = useRef<HTMLDivElement>(null);

  // Close on an outside click or Escape. Pointerdown rather than click so the
  // menu is gone before whatever you clicked acts on it.
  useEffect(() => {
    if (!open) return;
    function onPointer(e: PointerEvent) {
      if (!wrap.current?.contains(e.target as Node)) setOpen(false);
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("pointerdown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  function run(fn: () => void) {
    setOpen(false);
    fn();
  }

  return (
    <div ref={wrap} style={{ position: "relative", flexShrink: 0 }}>
      <button
        onClick={() => setOpen((v) => !v)}
        title="Settings"
        aria-label="Settings"
        aria-expanded={open}
        style={{
          display: "inline-flex",
          alignItems: "center",
          justifyContent: "center",
          width: 28,
          height: 28,
          padding: 0,
          borderRadius: 7,
          border: "1px solid " + (open ? "rgba(var(--accent2-rgb), 0.4)" : C.border),
          background: open ? "rgba(var(--accent2-rgb), 0.12)" : "transparent",
          color: open ? C.accent2 : C.textDim,
          cursor: "pointer",
          fontFamily: MONO,
          transition: "all 0.15s",
        }}
      >
        <svg
          width="15"
          height="15"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.7"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <circle cx="12" cy="12" r="3" />
          <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.6 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
        </svg>
      </button>

      {open && (
        <div style={menuStyle}>
          <MenuItem
            label="Reload"
            onClick={() => run(onReload)}
            icon={<path d="M21 12a9 9 0 1 1-2.64-6.36M21 3v6h-6" />}
          />
          <MenuItem
            label="New brain"
            onClick={() => run(onBack)}
            icon={<path d="M19 12H5M12 19l-7-7 7-7" />}
          />

          <div style={dividerStyle} />

          {/* The sun/moon switch it always was, just parked under the menu
              items rather than loose in the header. */}
          <div style={{ display: "flex", padding: "2px 10px 10px" }}>
            <ThemeToggle />
          </div>
        </div>
      )}
    </div>
  );
}

function MenuItem({
  label,
  onClick,
  icon,
}: {
  label: string;
  onClick: () => void;
  icon: ReactNode;
}) {
  return (
    <button onClick={onClick} style={itemStyle}>
      <svg
        width="14"
        height="14"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
        strokeLinejoin="round"
        style={{ flexShrink: 0, opacity: 0.85 }}
        aria-hidden="true"
      >
        {icon}
      </svg>
      <span style={{ color: "var(--text)" }}>{label}</span>
    </button>
  );
}

const menuStyle: CSSProperties = {
  position: "absolute",
  top: "calc(100% + 8px)",
  right: 0,
  zIndex: 40,
  // Sized to its longest label. It sits at the right edge of the header, so
  // every extra pixel of width is a pixel hanging off the viewport.
  width: 168,
  background: "var(--bg-overlay)",
  border: "1px solid var(--border-strong)",
  borderRadius: 11,
  padding: "6px 0 0",
  boxShadow: "0 18px 46px rgba(var(--shadow-rgb), 0.45)",
  WebkitBackdropFilter: "blur(18px)",
  backdropFilter: "blur(18px)",
};

const itemStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 9,
  width: "100%",
  padding: "8px 12px",
  background: "transparent",
  border: "none",
  color: "var(--text-dim)",
  fontFamily: MONO,
  fontSize: 14.5,
  textAlign: "left",
  cursor: "pointer",
};

const dividerStyle: CSSProperties = {
  height: 1,
  background: "var(--border)",
  margin: "6px 0 2px",
};

