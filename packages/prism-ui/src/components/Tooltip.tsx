import { C, MONO, colorFor } from "../theme";
import { receptorLabel } from "../types";
import type { NeuronView, ParticipantKind } from "../types";

/** The kind badge's tint. Written as a lookup rather than a chain of
 *  ternaries so adding a fifth primitive is a one-line change, not a
 *  re-reading of three parallel conditionals. */
const badgeTint = (kind: ParticipantKind) => {
  const rgb =
    kind === "engram" ? "--engram-rgb" :
    kind === "effector" ? "--effector-rgb" :
    kind === "receptor" ? "--receptor-rgb" :
    "--accent2-rgb";
  const fg =
    kind === "engram" ? C.accentText :
    kind === "effector" ? C.effector :
    kind === "receptor" ? C.receptor :
    C.accent2Text;
  return {
    color: fg,
    background: `rgba(var(${rgb}), 0.12)`,
    border: `1px solid rgba(var(${rgb}), 0.3)`,
  };
};

interface Props {
  neuron: NeuronView | null;
  x: number;
  y: number;
}

export function Tooltip({ neuron, x, y }: Props) {
  if (!neuron) return null;
  return (
    <div
      style={{
        position: "absolute",
        left: x + 18,
        top: y + 18,
        zIndex: 10,
        background: "var(--bg-panel)",
        border: "1px solid " + C.borderStrong,
        borderRadius: 10,
        padding: "12px 14px",
        minWidth: 240,
        maxWidth: 320,
        boxShadow: "0 30px 80px -20px rgba(var(--shadow-rgb), 0.6)",
        WebkitBackdropFilter: "blur(20px)",
        backdropFilter: "blur(20px)",
        pointerEvents: "none",
      }}
    >
      <div
        style={{
          fontFamily: MONO,
          fontSize: 14.5,
          color: C.accentText,
          fontWeight: 600,
          marginBottom: 6,
          wordBreak: "break-all",
        }}
      >
        {receptorLabel(neuron.id)}
      </div>
      <div
        style={{
          display: "inline-block",
          fontFamily: MONO,
          fontSize: 13,
          fontWeight: 600,
          letterSpacing: 0.4,
          textTransform: "uppercase",
          padding: "2px 7px",
          borderRadius: 4,
          marginBottom: 8,
          ...badgeTint(neuron.kind),
        }}
      >
        {neuron.kind}
      </div>
      {neuron.capabilities.length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginBottom: 8 }}>
          {neuron.capabilities.map((c) => (
            <span
              key={c}
              style={{
                fontSize: 13,
                fontFamily: MONO,
                padding: "2px 7px",
                borderRadius: 4,
                background: "rgba(var(--accent2-rgb), 0.08)",
                color: C.accent2Text,
                border: "1px solid rgba(var(--accent2-rgb), 0.2)",
              }}
            >
              {c}
            </span>
          ))}
        </div>
      )}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "auto 1fr",
          gap: "4px 12px",
          fontSize: 14,
          color: C.textDim, fontWeight: 600,
          fontFamily: MONO,
        }}
      >
        <span style={{ color: C.textFaint, fontWeight: 600, }}>signals</span>
        <span>{neuron.count}</span>
        {neuron.lastType && (
          <>
            <span style={{ color: C.textFaint, fontWeight: 600, }}>last</span>
            <span style={{ color: colorFor(neuron.lastType) }}>{neuron.lastType}</span>
          </>
        )}
        {neuron.lastTs && (
          <>
            <span style={{ color: C.textFaint, fontWeight: 600, }}>at</span>
            <span>{new Date(neuron.lastTs).toLocaleTimeString()}</span>
          </>
        )}
        {neuron.version && (
          <>
            <span style={{ color: C.textFaint, fontWeight: 600, }}>version</span>
            <span>{neuron.version}</span>
          </>
        )}
      </div>
    </div>
  );
}
