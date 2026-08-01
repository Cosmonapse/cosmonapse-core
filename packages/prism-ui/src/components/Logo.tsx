import mark from "../assets/mark.png";
import markLight from "../assets/mark-light.png";
import { useThemeMode } from "../theme";

interface Props {
  size?: number;
}

// The Cosmonapse mark (app/assets/mark.png from the landing page), used for the
// Prism header and connect-form lockup. Replaces the old CSS gradient square.
//
// mark-light.png is the same crop/framing, recolored to match the landing
// page's light-mode mark (app/assets/logowork-light.png — navy + vermillion,
// i.e. theme.ts's LIGHT.accent / LIGHT.accent3).
export function Logo({ size = 22 }: Props) {
  const mode = useThemeMode();
  const src = mode === "light" ? markLight : mark;

  return (
    <img
      src={src}
      width={size}
      height={size}
      alt="Cosmonapse"
      style={{
        display: "block",
        borderRadius: 6,
        filter: "drop-shadow(0 0 12px var(--glow))",
      }}
    />
  );
}
