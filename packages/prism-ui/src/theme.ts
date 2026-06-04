import type { SignalType } from "./types";

// Core palette — kept in sync with the CSS variables in styles.css.
export const C = {
  bg: "#07080c",
  bgCard: "#0f111a",
  border: "rgba(255,255,255,0.06)",
  borderStrong: "rgba(255,255,255,0.12)",
  text: "#e6e7ec",
  textDim: "#9097a8",
  textFaint: "#5b6275",
  accent: "#8b5cf6",
  accent2: "#22d3ee",
  accent3: "#f472b6",
  glow: "rgba(139,92,246,0.35)",
} as const;

export const MONO = "ui-monospace,Menlo,monospace";

const TYPE_COLOR: Record<string, string> = {
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
  TOOL_CALL: "#e2e8f0",
  TOOL_RESULT: "#e2e8f0",
  MEMORY_APPEND: "#22d3ee",
  ESCALATION: "#fb923c",
  CONSENSUS: "#06b6d4",
  CONTEXT_SYNC: "#22d3ee",
  CRITIQUE: "#fbbf24",
  DISCOVER: "#f472b6",
};

export function colorFor(type: SignalType | string | undefined): string {
  return (type && TYPE_COLOR[type]) || C.accent;
}
