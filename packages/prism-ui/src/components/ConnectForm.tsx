import { useState } from "react";
import { C, MONO } from "../theme";
import { Logo } from "./Logo";
import type { SynapseTarget } from "../useSignalStream";

interface Props {
  initial?: Partial<SynapseTarget>;
  onConnect: (target: SynapseTarget) => void;
  /** Shown when adding an extra tab — lets the user return to open sessions. */
  onCancel?: () => void;
}

export function ConnectForm({ initial, onConnect, onCancel }: Props) {
  const [url, setUrl] = useState(initial?.url ?? "cosmo://127.0.0.1:7070");
  const [namespace, setNamespace] = useState(initial?.namespace ?? "dev");

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const u = url.trim();
    if (!u) return;
    onConnect({ url: u, namespace: namespace.trim() || "dev" });
  };

  return (
    <div
      style={{
        height: "100vh",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: 24,
      }}
    >
      <form
        onSubmit={submit}
        style={{
          width: "100%",
          maxWidth: 440,
          background: "rgba(15,17,26,0.7)",
          border: "1px solid " + C.borderStrong,
          borderRadius: 16,
          padding: 28,
          backdropFilter: "blur(20px)",
          WebkitBackdropFilter: "blur(20px)",
          boxShadow: "0 40px 120px -30px rgba(0,0,0,0.7)",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 6 }}>
          <Logo size={32} />
          <div>
            <div style={{ fontWeight: 700, fontSize: 18 }}>
              <span className="brand-word">Cosmonapse</span>{" "}
              <span style={{ color: C.textDim, fontWeight: 500 }}>Prism</span>
            </div>
          </div>
        </div>
        <p style={{ color: C.textDim, fontSize: 13, margin: "8px 0 22px" }}>
          Drop a synapse link. Prism attaches a read-only Doppler and visualizes every
          signal live.
        </p>

        <label style={labelStyle}>Synapse URL</label>
        <input
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="cosmo://127.0.0.1:7070"
          spellCheck={false}
          autoFocus
          style={inputStyle}
        />

        <label style={{ ...labelStyle, marginTop: 16 }}>Namespace</label>
        <input
          value={namespace}
          onChange={(e) => setNamespace(e.target.value)}
          placeholder="dev"
          spellCheck={false}
          style={inputStyle}
        />

        <button type="submit" style={connectBtn}>
          Attach Prism →
        </button>

        {onCancel && (
          <button
            type="button"
            onClick={onCancel}
            style={{
              width: "100%",
              marginTop: 10,
              background: "transparent",
              border: "1px solid " + C.borderStrong,
              borderRadius: 10,
              padding: "10px 16px",
              color: C.textDim,
              fontSize: 13,
              cursor: "pointer",
            }}
          >
            ← back to open synapses
          </button>
        )}

        <div style={{ color: C.textFaint, fontFamily: MONO, fontSize: 11, marginTop: 16 }}>
          Schemes: cosmo:// · nats:// · kafka://
        </div>
      </form>
    </div>
  );
}

const labelStyle: React.CSSProperties = {
  display: "block",
  fontFamily: MONO,
  fontSize: 11,
  letterSpacing: "0.12em",
  textTransform: "uppercase",
  color: C.textFaint,
  marginBottom: 7,
};

const inputStyle: React.CSSProperties = {
  width: "100%",
  background: "rgba(0,0,0,0.35)",
  border: "1px solid " + C.borderStrong,
  borderRadius: 9,
  padding: "11px 13px",
  color: C.text,
  fontFamily: MONO,
  fontSize: 13.5,
  outline: "none",
};

const connectBtn: React.CSSProperties = {
  width: "100%",
  marginTop: 24,
  background: "linear-gradient(135deg,#8b5cf6,#7c3aed)",
  border: "none",
  borderRadius: 10,
  padding: "12px 16px",
  color: "#fff",
  fontSize: 14,
  fontWeight: 600,
  cursor: "pointer",
  boxShadow: "0 10px 30px -8px " + C.glow,
};
