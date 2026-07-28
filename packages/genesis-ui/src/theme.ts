// Core palette  -  kept in sync with the CSS variables in styles.css, and
// with Prism's node colours (packages/prism-ui/src/theme.ts /
// components/PrismCanvas.tsx) so a Neuron/Effector/Engram looks the same
// whether you're watching it live in Prism or laying it out in Genesis.
export const C = {
  bg: "#07080c",
  bgCard: "#0f111a",
  bgElev: "#0c0e15",
  border: "rgba(255,255,255,0.06)",
  borderStrong: "rgba(255,255,255,0.12)",
  text: "#e6e7ec",
  textDim: "#9097a8",
  textFaint: "#5b6275",
  accent: "#8b5cf6",
  accent2: "#22d3ee",
  accent3: "#f472b6",
  glow: "rgba(139,92,246,0.35)",

  synapse: "#22d3ee",
  neuron: "#8b5cf6",
  effector: "#f59e0b",
  engram: "#a78bfa",
} as const;

export const MONO = "ui-monospace,Menlo,monospace";
