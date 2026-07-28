import { useEffect, useRef, useState } from "react";
import { C, MONO } from "../theme";
import { shortUrl, type SynapseTab } from "../tabs";

interface Props {
  tabs: SynapseTab[];
  activeId: string | null;
  /** tab id → live connection state, so the menu shows every synapse at a glance. */
  statuses: Record<string, boolean>;
  onSelect: (id: string) => void;
  onAdd: () => void;
  onClose: (id: string) => void;
}

function Dot({ on }: { on: boolean }) {
  return (
    <span
      style={{
        width: 6,
        height: 6,
        borderRadius: "50%",
        flexShrink: 0,
        background: on ? "#34d399" : "#f87171",
        boxShadow: `0 0 5px ${on ? "#34d399" : "#f87171"}`,
      }}
    />
  );
}

/**
 * The synapse selector that sits next to the wordmark: the active
 * url/namespace as a dropdown over every open synapse, plus a + to attach
 * another. Each entry is its own live session - switching never drops a stream.
 */
export function SynapseSwitcher({ tabs, activeId, statuses, onSelect, onAdd, onClose }: Props) {
  const [open, setOpen] = useState(false);
  const [hovered, setHovered] = useState<string | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const esc = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);

  const active = tabs.find((t) => t.id === activeId) ?? null;
  if (!active) return null;

  return (
    <div
      ref={rootRef}
      style={{ position: "relative", display: "flex", alignItems: "center", gap: 6, flexShrink: 0 }}
    >
      <div
        onClick={() => setOpen((o) => !o)}
        title={`${shortUrl(active.url)} /${active.namespace} — switch synapse`}
        style={{
          display: "flex",
          alignItems: "center",
          gap: 7,
          padding: "4px 9px 4px 10px",
          borderRadius: 8,
          cursor: "pointer",
          fontFamily: MONO,
          fontSize: 12,
          background: "rgba(139,92,246,0.13)",
          border: "1px solid rgba(139,92,246,0.45)",
          transition: "all 0.15s",
        }}
      >
        <Dot on={!!statuses[active.id]} />
        <span style={{ color: C.accent2, whiteSpace: "nowrap" }}>{shortUrl(active.url)}</span>
        <span style={{ color: C.textDim, whiteSpace: "nowrap" }}>/{active.namespace}</span>
        {tabs.length > 1 && (
          <span
            style={{
              color: C.textFaint,
              fontSize: 10,
              padding: "1px 5px",
              borderRadius: 20,
              background: "rgba(255,255,255,0.07)",
            }}
          >
            {tabs.length}
          </span>
        )}
        <span
          style={{
            color: C.textFaint,
            fontSize: 9,
            transform: open ? "rotate(180deg)" : "none",
            transition: "transform 0.15s",
          }}
        >
          ▼
        </span>
      </div>

      <div
        onClick={onAdd}
        title="Attach another synapse"
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          width: 26,
          height: 26,
          borderRadius: 8,
          cursor: "pointer",
          fontSize: 15,
          lineHeight: 1,
          color: C.textDim,
          border: "1px solid " + C.borderStrong,
          background: "transparent",
          transition: "all 0.15s",
        }}
      >
        +
      </div>

      {open && (
        <div
          style={{
            position: "absolute",
            top: "calc(100% + 9px)",
            left: 0,
            zIndex: 40,
            minWidth: 290,
            padding: 6,
            borderRadius: 12,
            background: "rgba(15,17,26,0.94)",
            border: "1px solid " + C.borderStrong,
            backdropFilter: "blur(20px)",
            WebkitBackdropFilter: "blur(20px)",
            boxShadow: "0 30px 80px -24px rgba(0,0,0,0.8)",
          }}
        >
          <div
            style={{
              fontFamily: MONO,
              fontSize: 10,
              letterSpacing: "0.12em",
              textTransform: "uppercase",
              color: C.textFaint,
              padding: "6px 9px 8px",
            }}
          >
            Open synapses
          </div>

          {tabs.map((t) => {
            const on = t.id === activeId;
            return (
              <div
                key={t.id}
                onClick={() => {
                  onSelect(t.id);
                  setOpen(false);
                }}
                onMouseEnter={() => setHovered(t.id)}
                onMouseLeave={() => setHovered(null)}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                  padding: "7px 9px",
                  borderRadius: 8,
                  cursor: "pointer",
                  fontFamily: MONO,
                  fontSize: 12,
                  background: on
                    ? "rgba(139,92,246,0.16)"
                    : hovered === t.id
                      ? "rgba(255,255,255,0.05)"
                      : "transparent",
                  border: "1px solid " + (on ? "rgba(139,92,246,0.4)" : "transparent"),
                }}
              >
                <Dot on={!!statuses[t.id]} />
                <span style={{ color: on ? C.accent2 : C.text, whiteSpace: "nowrap" }}>
                  {shortUrl(t.url)}
                </span>
                <span style={{ color: C.textDim, whiteSpace: "nowrap" }}>/{t.namespace}</span>
                <span
                  onClick={(e) => {
                    e.stopPropagation();
                    onClose(t.id);
                  }}
                  title="Detach this synapse"
                  style={{
                    marginLeft: "auto",
                    paddingLeft: 10,
                    fontSize: 13,
                    lineHeight: 1,
                    color: hovered === t.id ? C.textDim : "transparent",
                  }}
                >
                  ×
                </span>
              </div>
            );
          })}

          <div style={{ height: 1, background: C.border, margin: "6px 4px" }} />

          <div
            onClick={() => {
              onAdd();
              setOpen(false);
            }}
            onMouseEnter={() => setHovered("__add")}
            onMouseLeave={() => setHovered(null)}
            style={{
              display: "flex",
              alignItems: "center",
              gap: 8,
              padding: "7px 9px",
              borderRadius: 8,
              cursor: "pointer",
              fontFamily: MONO,
              fontSize: 12,
              color: C.accent2,
              background: hovered === "__add" ? "rgba(34,211,238,0.1)" : "transparent",
            }}
          >
            <span style={{ fontSize: 14, lineHeight: 1 }}>+</span>
            Attach another synapse
          </div>
        </div>
      )}
    </div>
  );
}
