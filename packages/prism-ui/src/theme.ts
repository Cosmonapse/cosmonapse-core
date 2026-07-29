import { useSyncExternalStore } from "react";
import type { SignalType } from "./types";

export type ThemeMode = "dark" | "light";

/** Prism keeps its own key: it and Genesis run on different ports. */
export const THEME_STORAGE_KEY = "cosmonapse-prism-theme";

/**
 * The palette is the single source of truth for colour in Prism.
 *
 * Two consumers read it, and they need different things:
 *
 *   - SVG presentation attributes (`fill={C.text}`) cannot resolve `var()`,
 *     so they need literal values. "C" is a *mutable* object holding the
 *     active theme's literals; flipping the theme re-renders the tree from
 *     the root, so every `C.x` read picks up the new value.
 *   - CSS and inline `style={{}}` objects declared at module scope freeze at
 *     import time, so those use `var(--token)` strings, which the browser
 *     resolves live. `applyTheme` publishes every palette entry as a custom
 *     property on <html>, so the two stay in lockstep by construction.
 */
type Palette = {
  bg: string;
  bgCard: string;
  bgElev: string;
  /** Recessed well: <pre> blocks, inputs. */
  bgWell: string;
  /** Left rails and list gutters. */
  bgRail: string;
  /** Floating panels, menus, tooltips. */
  bgPanel: string;
  /** Full-bleed view background behind the canvas. */
  bgView: string;
  /** Translucent chrome: header, sidebar, modal scrims. */
  bgHeader: string;
  bgSidebar: string;
  bgOverlay: string;
  border: string;
  borderStrong: string;
  text: string;
  textDim: string;
  textFaint: string;
  /** Deliberately low-salience grey for "other"/aggregate rows. */
  muted: string;
  accent: string;
  accent2: string;
  accent3: string;
  /** Accent-tinted text that has to stay legible on an accent wash. */
  accentText: string;
  accent2Text: string;
  /** Text on a *solid* accent fill. */
  onAccent: string;
  gradAccent: string;
  glow: string;
  ok: string;
  okSoft: string;
  warn: string;
  danger: string;
  dangerText: string;
  engram: string;
  /** IMPRINT / IMPRINTED  -  the write side of Engram. */
  imprint: string;
  effector: string;
  neuron: string;
  /** Receptor - the listening edge. The only free hue left once neuron and
   *  engram took violet, effector amber and synapse cyan. */
  receptor: string;
  synapse: string;
  scrollThumb: string;
  scrollThumbHover: string;
  /** The travelling spark on a signal edge. */
  spark: string;
  /* Channel triplets: alpha washes are written as
     "rgba(var(--fg-rgb), 0.04)" so a theme restates the triplet, not the
     dozens of rgba() calls that consume it. */
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
  bgWell: "rgba(0,0,0,0.35)",
  bgRail: "rgba(0,0,0,0.2)",
  bgPanel: "rgba(15,17,26,0.94)",
  bgView: "rgba(7,8,12,0.6)",
  bgHeader: "rgba(7,8,12,0.7)",
  bgSidebar: "rgba(7,8,12,0.85)",
  bgOverlay: "rgba(7,8,12,0.92)",
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
  gradAccent: "linear-gradient(135deg,#8b5cf6,#7c3aed)",
  glow: "rgba(139,92,246,0.35)",
  ok: "#10b981",
  okSoft: "#34d399",
  warn: "#fbbf24",
  danger: "#f87171",
  dangerText: "#fecaca",
  engram: "#a78bfa",
  imprint: "#c084fc",
  effector: "#f59e0b",
  neuron: "#8b5cf6",
  receptor: "#a3e635",
  synapse: "#22d3ee",
  scrollThumb: "#1e2433",
  scrollThumbHover: "#2a3146",
  spark: "#ffffff",
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
 * Light theme. Ground is white with #25507a as the primary, matching the
 * Cosmonapse landing page and the light logo mark.
 *
 * "accent2" stays in the cyan family (deep teal) rather than taking the
 * brand vermillion: it marks the active view and selected rows all over
 * Prism, and a red-orange there would read as an error state in a tool
 * whose whole job is surfacing errors. The vermillion lives on "accent3".
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
  bgSidebar: "rgba(255,255,255,0.9)",
  bgOverlay: "rgba(255,255,255,0.95)",
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
  gradAccent: "linear-gradient(135deg,#25507a,#1d4066)",
  glow: "rgba(37,80,122,0.16)",
  ok: "#15803d",
  okSoft: "#047857",
  warn: "#b45309",
  danger: "#c02626",
  dangerText: "#7f1d1d",
  engram: "#6d28d9",
  imprint: "#9333ea",
  effector: "#b45309",
  neuron: "#6d28d9",
  receptor: "#4d7c0f",
  synapse: "#0e7490",
  scrollThumb: "#cbd5e1",
  scrollThumbHover: "#94a3b8",
  spark: "#1d4066",
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

/** Live palette. Mutated in place by `applyTheme`  -  never reassigned. */
export const C: Palette = { ...DARK };

export const MONO = "ui-monospace,Menlo,monospace";

const DARK_TYPE_COLOR: Record<string, string> = {
  TASK: "#22d3ee",
  AGENT_OUTPUT: "#34d399",
  FINAL: "#10b981",
  ERROR: "#f87171",
  CLARIFICATION: "#fbbf24",
  REGISTER: "#8b5cf6",
  DEREGISTER: "#7c3aed",
  HEARTBEAT: "#475569",
  TASK_OFFER: "#c084fc",
  BID: "#c084fc",
  TASK_AWARDED: "#a855f7",
  TASK_DECLINED: "#7c3aed",
  THOUGHT_DELTA: "#64748b",
  PLAN: "#94a3b8",
  // Effector (tools act) - kept in sync with "effectorColor" in
  // PrismCanvas.tsx, the same way RECALL/RECALLED match "engramColor".
  TOOL_CALL: "#f59e0b",
  TOOL_RESULT: "#f59e0b",
  MEMORY_APPEND: "#22d3ee",
  ESCALATION: "#fb923c",
  CONSENSUS: "#06b6d4",
  CONTEXT_SYNC: "#22d3ee",
  CRITIQUE: "#fbbf24",
  DISCOVER: "#f472b6",
  // Engram
  RECALL: "#a78bfa",
  RECALLED: "#a78bfa",
  IMPRINT: "#c084fc",
  IMPRINTED: "#c084fc",
  // Clarification / permission
  PERMISSION: "#fbbf24",
  PERMISSION_DECISION: "#d97706",
  CLARIFICATION_ANSWER: "#d97706",
};

/**
 * Same hue for every type, darkened until it holds up on white. The two
 * deliberately quiet types (HEARTBEAT, THOUGHT_DELTA) stay low-contrast in
 * both themes  -  they are background noise by design.
 */
const LIGHT_TYPE_COLOR: Record<string, string> = {
  TASK: "#0e7490",
  AGENT_OUTPUT: "#047857",
  FINAL: "#15803d",
  ERROR: "#c02626",
  CLARIFICATION: "#b45309",
  REGISTER: "#6d28d9",
  DEREGISTER: "#5b21b6",
  HEARTBEAT: "#a3b1c2",
  TASK_OFFER: "#9333ea",
  BID: "#9333ea",
  TASK_AWARDED: "#7e22ce",
  TASK_DECLINED: "#5b21b6",
  THOUGHT_DELTA: "#a3b1c2",
  PLAN: "#64748b",
  TOOL_CALL: "#b45309",
  TOOL_RESULT: "#b45309",
  MEMORY_APPEND: "#0e7490",
  ESCALATION: "#c2410c",
  CONSENSUS: "#0b5c73",
  CONTEXT_SYNC: "#0e7490",
  CRITIQUE: "#b45309",
  DISCOVER: "#be185d",
  RECALL: "#6d28d9",
  RECALLED: "#6d28d9",
  IMPRINT: "#9333ea",
  IMPRINTED: "#9333ea",
  PERMISSION: "#b45309",
  PERMISSION_DECISION: "#92400e",
  CLARIFICATION_ANSWER: "#92400e",
};

const TYPE_COLOR: Record<string, string> = { ...DARK_TYPE_COLOR };

export function colorFor(type: SignalType | string | undefined): string {
  return (type && TYPE_COLOR[type]) || C.accent;
}

/* ──────────────────────────  Theme store  ────────────────────────── */

const kebab = (k: string) => "--" + k.replace(/[A-Z]/g, (m) => "-" + m.toLowerCase());

let mode: ThemeMode = "dark";
const listeners = new Set<() => void>();

function paint(next: ThemeMode) {
  const p = next === "light" ? LIGHT : DARK;
  Object.assign(C, p);
  Object.assign(TYPE_COLOR, next === "light" ? LIGHT_TYPE_COLOR : DARK_TYPE_COLOR);
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
 * including SVG `fill=` attributes  -  refreshes.
 */
export function useThemeMode(): ThemeMode {
  return useSyncExternalStore(subscribe, getMode, getMode);
}

// Applied at import time, before React first renders, so there is no flash.
paint(readStoredTheme());
