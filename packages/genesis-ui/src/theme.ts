import { useSyncExternalStore } from "react";

export type ThemeMode = "dark" | "light";

/** Genesis keeps its own key: it and Prism run on different ports. */
export const THEME_STORAGE_KEY = "cosmonapse-genesis-theme";

/**
 * The palette is the single source of truth for colour in Genesis, and it
 * mirrors Prism's (packages/prism-ui/src/theme.ts) so a Neuron / Effector /
 * Engram looks the same whether you're watching it live or laying it out.
 *
 * Two consumers read it, and they need different things:
 *
 *   - SVG presentation attributes (`stroke={kindColor().neuron}`) cannot
 *     resolve `var()`, so they need literal values. "C" is a *mutable* object
 *     holding the active theme's literals; a theme flip re-renders from the
 *     root, so every `C.x` read picks up the new value.
 *   - CSS, and inline `style={{}}` objects declared at module scope, freeze at
 *     import time, so those use `var(--token)` strings. `applyTheme` publishes
 *     every palette entry as a custom property on <html>, so the two stay in
 *     lockstep by construction.
 */
type Palette = {
  bg: string;
  bgCard: string;
  bgElev: string;
  /** Recessed well: <pre> blocks, inputs. */
  bgWell: string;
  bgRail: string;
  /** Floating panels, menus, popovers. */
  bgPanel: string;
  bgView: string;
  bgHeader: string;
  bgOverlay: string;
  border: string;
  borderStrong: string;
  text: string;
  textDim: string;
  textFaint: string;
  muted: string;
  accent: string;
  accent2: string;
  accent3: string;
  accentText: string;
  accent2Text: string;
  /** Text on a solid accent fill. */
  onAccent: string;
  /** Text on the bright accent→accent2 gradient of a primary button. */
  onPrimary: string;
  glow: string;
  ok: string;
  okSoft: string;
  warn: string;
  danger: string;
  /* Component kinds  -  shared vocabulary with Prism. */
  synapse: string;
  neuron: string;
  effector: string;
  engram: string;
  /** Receptor - the listening edge. The only free hue left once neuron and
   *  engram took violet, effector amber and synapse cyan. */
  receptor: string;
  /* Python syntax tokens in the code preview. */
  tkString: string;
  tkNumber: string;
  scrollThumb: string;
  scrollThumbHover: string;
  /* Channel triplets for alpha washes. */
  fgRgb: string;
  bgRgb: string;
  surfaceRgb: string;
  shadowRgb: string;
  accentRgb: string;
  accent2Rgb: string;
  accent3Rgb: string;
  engramRgb: string;
  effectorRgb: string;
  receptorRgb: string;
  dangerRgb: string;
};

const DARK: Palette = {
  bg: "#07080c",
  bgCard: "#0f111a",
  bgElev: "#0c0e15",
  bgWell: "rgba(0,0,0,0.15)",
  bgRail: "rgba(0,0,0,0.2)",
  bgPanel: "rgba(15,17,26,0.92)",
  bgView: "rgba(7,8,12,0.6)",
  bgHeader: "rgba(7,8,12,0.7)",
  bgOverlay: "rgba(15,17,26,0.95)",
  border: "rgba(255,255,255,0.06)",
  borderStrong: "rgba(255,255,255,0.12)",
  text: "#e6e7ec",
  textDim: "#9097a8",
  textFaint: "#838aa0",
  muted: "#8089a0",
  accent: "#8b5cf6",
  accent2: "#22d3ee",
  accent3: "#f472b6",
  accentText: "#c4b5fd",
  accent2Text: "#67e8f9",
  onAccent: "#ffffff",
  onPrimary: "#0a0b10",
  glow: "rgba(139,92,246,0.35)",
  ok: "#10b981",
  okSoft: "#34d399",
  warn: "#fbbf24",
  danger: "#f87171",
  synapse: "#22d3ee",
  neuron: "#8b5cf6",
  effector: "#f59e0b",
  engram: "#a78bfa",
  receptor: "#a3e635",
  tkString: "#7dd3a0",
  tkNumber: "#f0a868",
  scrollThumb: "#1e2433",
  scrollThumbHover: "#2a3146",
  fgRgb: "255,255,255",
  bgRgb: "7,8,12",
  surfaceRgb: "15,17,26",
  shadowRgb: "0,0,0",
  accentRgb: "139,92,246",
  accent2Rgb: "34,211,238",
  accent3Rgb: "244,114,182",
  engramRgb: "167,139,250",
  effectorRgb: "245,158,11",
  receptorRgb: "163,230,53",
  dangerRgb: "248,113,113",
};

/**
 * Light theme. White ground with #25507a as the primary, matching the
 * Cosmonapse landing page and the light logo mark.
 *
 * "accent2" stays in the cyan family (deep teal) rather than taking the brand
 * vermillion: it marks selected nodes and active chips throughout the editor,
 * and a red-orange there would read as an error. The vermillion is "accent3".
 */
const LIGHT: Palette = {
  bg: "#ffffff",
  bgCard: "#f8fafc",
  bgElev: "#f4f7fa",
  bgWell: "#eef2f7",
  bgRail: "#f4f7fa",
  bgPanel: "rgba(255,255,255,0.97)",
  bgView: "rgba(248,250,252,0.85)",
  bgHeader: "rgba(255,255,255,0.8)",
  bgOverlay: "rgba(255,255,255,0.97)",
  border: "rgba(37,80,122,0.14)",
  borderStrong: "rgba(37,80,122,0.24)",
  text: "#16324c",
  textDim: "#4d6580",
  textFaint: "#4a6178",
  muted: "#4e5c72",
  accent: "#25507a",
  accent2: "#0e7490",
  accent3: "#c4442a",
  accentText: "#1d4066",
  accent2Text: "#0b5c73",
  onAccent: "#ffffff",
  onPrimary: "#ffffff",
  glow: "rgba(37,80,122,0.16)",
  ok: "#15803d",
  okSoft: "#047857",
  warn: "#b45309",
  danger: "#c02626",
  synapse: "#0e7490",
  neuron: "#6d28d9",
  effector: "#b45309",
  engram: "#6d28d9",
  receptor: "#4d7c0f",
  tkString: "#15803d",
  tkNumber: "#b45309",
  scrollThumb: "#cbd5e1",
  scrollThumbHover: "#94a3b8",
  fgRgb: "37,80,122",
  bgRgb: "255,255,255",
  surfaceRgb: "244,247,250",
  shadowRgb: "22,47,74",
  accentRgb: "37,80,122",
  accent2Rgb: "14,116,144",
  accent3Rgb: "196,68,42",
  engramRgb: "109,40,217",
  effectorRgb: "180,83,9",
  receptorRgb: "77,124,15",
  dangerRgb: "192,38,38",
};

/** Live palette. Mutated in place by the store  -  never reassigned. */
export const C: Palette = { ...DARK };

export const MONO = "ui-monospace,Menlo,monospace";

/* ──────────────────────────  Theme store  ────────────────────────── */

const kebab = (k: string) =>
  "--" + k.replace(/[A-Z]/g, (m) => "-" + m.toLowerCase()).replace(/-(\d)/g, "$1");

let mode: ThemeMode = "dark";
const listeners = new Set<() => void>();

function paint(next: ThemeMode) {
  const p = next === "light" ? LIGHT : DARK;
  Object.assign(C, p);
  if (typeof document !== "undefined") {
    const root = document.documentElement;
    root.setAttribute("data-theme", next);
    for (const [k, v] of Object.entries(p)) root.style.setProperty(kebab(k), v);
  }
  mode = next;
}

export function readStoredTheme(): ThemeMode {
  try {
    return localStorage.getItem(THEME_STORAGE_KEY) === "light" ? "light" : "dark";
  } catch {
    return "dark";
  }
}

export function setTheme(next: ThemeMode) {
  paint(next);
  try {
    localStorage.setItem(THEME_STORAGE_KEY, next);
  } catch {
    /* private mode  -  the theme still applies for this session */
  }
  listeners.forEach((l) => l());
}

export function toggleTheme() {
  setTheme(mode === "light" ? "dark" : "light");
}

const subscribe = (l: () => void) => {
  listeners.add(l);
  return () => void listeners.delete(l);
};
const getMode = () => mode;

/**
 * Call once near the root. It returns the mode, but its real job is to make
 * the whole tree re-render on a theme flip so every literal `C.x` read  -
 * including SVG stroke=/fill= attributes  -  refreshes.
 */
export function useThemeMode(): ThemeMode {
  return useSyncExternalStore(subscribe, getMode, getMode);
}

// Applied at import time, before React first renders, so there is no flash.
paint(readStoredTheme());
