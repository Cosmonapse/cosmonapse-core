import { C, MONO } from "../theme";
import { Logo } from "./Logo";
import { SettingsMenu } from "./SettingsMenu";
import { SynapseIndicator } from "./SynapseIndicator";

// Which pane of the workspace is in front. "canvas" is the draw.io-style
// layout, "code" the source browser over the same components, and "test" the
// place you actually run the thing and talk to its Receptors.
export type GenesisView = "canvas" | "code" | "test";

export const VIEWS: { id: GenesisView; label: string }[] = [
  { id: "canvas", label: "Canvas" },
  { id: "code", label: "Code" },
  { id: "test", label: "Test" },
];

interface Props {
  project: string | null;
  path: string | null;
  /** config.py's NAMESPACE - what the synapse indicator watches for. */
  namespace: string | null;
  view: GenesisView;
  onSelectView: (v: GenesisView) => void;
  onReload: () => void;
  onBack: () => void;
}

/**
 * The Genesis header, built to the same lockup as prism-ui's Header: the
 * Cosmonapse mark, the brand word in Michroma, the product name dim beside
 * it, then a pill view-switcher. Kept visually identical on purpose - the
 * two apps are one product and should read that way.
 *
 * The right-hand side holds the two things that change while you work: is
 * this brain's synapse live, and the gear that hides everything which isn't
 * (reload, new brain, theme).
 */
export function Header({
  project,
  path,
  namespace,
  view,
  onSelectView,
  onReload,
  onBack,
}: Props) {
  return (
    <div
      style={{
        position: "relative",
        zIndex: 5,
        display: "flex",
        alignItems: "center",
        gap: 14,
        padding: "12px 20px",
        background: "var(--bg-header)",
        WebkitBackdropFilter: "blur(20px)",
        backdropFilter: "blur(20px)",
        borderBottom: "1px solid " + C.border,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexShrink: 0 }}>
        <Logo size={30} />
        <span className="brand-word" style={{ fontWeight: 700, fontSize: 18 }}>
          Cosmonapse
        </span>
        <span style={{ color: C.textDim, fontWeight: 500, fontSize: 18 }}>Genesis</span>
      </div>

      <span style={{ color: C.textFaint, fontWeight: 600, flexShrink: 0 }}>│</span>

      {/* Canvas / Code / Test - three lenses on one project */}
      <div style={{ display: "flex", alignItems: "center", gap: 6, flexShrink: 0 }}>
        {VIEWS.map((v) => {
          const on = v.id === view;
          return (
            <div
              key={v.id}
              onClick={() => onSelectView(v.id)}
              style={{
                flexShrink: 0,
                padding: "4px 14px",
                borderRadius: 8,
                cursor: "pointer",
                fontFamily: MONO,
                fontSize: 14.5,
                whiteSpace: "nowrap",
                color: on ? C.accent2 : C.textDim,
                background: on ? "rgba(var(--accent2-rgb), 0.12)" : "transparent",
                border: "1px solid " + (on ? "rgba(var(--accent2-rgb), 0.4)" : C.border),
                transition: "all 0.15s",
              }}
            >
              {v.label}
            </div>
          );
        })}
      </div>

      {project && (
        <>
          <span style={{ color: C.textFaint, fontWeight: 600, flexShrink: 0 }}>│</span>
          <div
            style={{
              fontFamily: MONO,
              fontSize: 14.5,
              color: C.textDim, fontWeight: 600,
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
            title={path ?? undefined}
          >
            {project} <span style={{ color: C.textFaint, fontWeight: 600, }}>· {path}</span>
          </div>
        </>
      )}

      <div
        style={{
          marginLeft: "auto",
          display: "flex",
          alignItems: "center",
          gap: 10,
          flexShrink: 0,
        }}
      >
        {path && <SynapseIndicator projectPath={path} namespace={namespace} />}
        <SettingsMenu onReload={onReload} onBack={onBack} />
      </div>
    </div>
  );
}
