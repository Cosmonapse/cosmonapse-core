import { C, MONO, colorFor } from "../theme";
import type { NeuronView } from "../types";

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
        background: "rgba(15,17,26,0.96)",
        border: "1px solid " + C.borderStrong,
        borderRadius: 10,
        padding: "12px 14px",
        minWidth: 240,
        maxWidth: 320,
        boxShadow: "0 30px 80px -20px rgba(0,0,0,0.6)",
        WebkitBackdropFilter: "blur(20px)",
        backdropFilter: "blur(20px)",
        pointerEvents: "none",
      }}
    >
      <div
        style={{
          fontFamily: MONO,
          fontSize: 12.5,
          color: "#c4b5fd",
          fontWeight: 600,
          marginBottom: 6,
          wordBreak: "break-all",
        }}
      >
        {neuron.id}
      </div>
      {neuron.capabilities.length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginBottom: 8 }}>
          {neuron.capabilities.map((c) => (
            <span
              key={c}
              style={{
                fontSize: 10.5,
                fontFamily: MONO,
                padding: "2px 7px",
                borderRadius: 4,
                background: "rgba(34,211,238,0.08)",
                color: "#67e8f9",
                border: "1px solid rgba(34,211,238,0.2)",
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
          fontSize: 11.5,
          color: C.textDim,
          fontFamily: MONO,
        }}
      >
        <span style={{ color: C.textFaint }}>signals</span>
        <span>{neuron.count}</span>
        {neuron.lastType && (
          <>
            <span style={{ color: C.textFaint }}>last</span>
            <span style={{ color: colorFor(neuron.lastType) }}>{neuron.lastType}</span>
          </>
        )}
        {neuron.lastTs && (
          <>
            <span style={{ color: C.textFaint }}>at</span>
            <span>{new Date(neuron.lastTs).toLocaleTimeString()}</span>
          </>
        )}
        {neuron.version && (
          <>
            <span style={{ color: C.textFaint }}>version</span>
            <span>{neuron.version}</span>
          </>
        )}
      </div>
    </div>
  );
}
