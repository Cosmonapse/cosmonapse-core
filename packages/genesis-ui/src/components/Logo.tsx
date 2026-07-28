export function Logo() {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
      <div className="genesis-logo" />
      <span className="brand-word" style={{ fontSize: 14, color: "var(--text)" }}>
        GENESIS
      </span>
    </div>
  );
}
