import mark from "../assets/mark.png";

interface Props {
  size?: number;
}

// The Cosmonapse mark (app/assets/mark.png from the landing page), used for the
// Prism header and connect-form lockup. Replaces the old CSS gradient square.
export function Logo({ size = 22 }: Props) {
  return (
    <img
      src={mark}
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
