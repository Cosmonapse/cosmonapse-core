import { C, MONO, toggleTheme, useThemeMode } from "../theme";

/**
 * Sun/moon switch. Reads the store so the button itself re-renders, but the
 * store is what re-renders the rest of the tree.
 */
export function ThemeToggle() {
  const mode = useThemeMode();
  const light = mode === "light";

  return (
    <button
      onClick={toggleTheme}
      title={light ? "Switch to dark theme" : "Switch to light theme"}
      aria-label={light ? "Switch to dark theme" : "Switch to light theme"}
      style={{
        display: "inline-flex",
        alignItems: "center",
        justifyContent: "center",
        width: 28,
        height: 28,
        flexShrink: 0,
        padding: 0,
        borderRadius: 7,
        border: "1px solid " + C.border,
        background: "transparent",
        color: C.textDim, fontWeight: 600,
        cursor: "pointer",
        fontFamily: MONO,
        transition: "color 0.15s, border-color 0.15s, background 0.15s",
      }}
    >
      <svg
        width="15"
        height="15"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
      >
        {light ? (
          <>
            <circle cx="12" cy="12" r="4" />
            <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41" />
          </>
        ) : (
          <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
        )}
      </svg>
    </button>
  );
}
