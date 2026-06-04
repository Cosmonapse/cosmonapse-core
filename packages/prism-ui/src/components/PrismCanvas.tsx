import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
} from "react";
import { C, MONO, colorFor } from "../theme";
import { AXON_TYPES, SYNAPSE_NODE, TARGET_TYPES } from "../types";
import type { NeuronView, Signal } from "../types";

export interface PrismCanvasHandle {
  /** Trigger transient animation (pulses + particle) for a signal. */
  emit: (sig: Signal) => void;
}

interface Props {
  neurons: Map<string, NeuronView>;
  namespace: string;
  sidebarOffset: number;
  onHover: (id: string | null) => void;
}

type Point = { x: number; y: number };
type Particle = { id: string; from: string; to: string; color: string };

const PULSE_MS = 800;
const PARTICLE_MS = 1100;

export const PrismCanvas = forwardRef<PrismCanvasHandle, Props>(function PrismCanvas(
  { neurons, namespace, sidebarOffset, onHover },
  ref,
) {
  const [vp, setVp] = useState({ w: window.innerWidth, h: window.innerHeight - 64 });
  const [pulses, setPulses] = useState<Set<string>>(() => new Set());
  const [tendrilsOn, setTendrilsOn] = useState<Set<string>>(() => new Set());
  const [particles, setParticles] = useState<Particle[]>([]);

  const pulseTimers = useRef(new Map<string, ReturnType<typeof setTimeout>>());
  const tendrilTimers = useRef(new Map<string, ReturnType<typeof setTimeout>>());

  useEffect(() => {
    const r = () => setVp({ w: window.innerWidth, h: window.innerHeight - 64 });
    window.addEventListener("resize", r);
    return () => window.removeEventListener("resize", r);
  }, []);

  const pulse = useCallback((id: string) => {
    setPulses((p) => new Set(p).add(id));
    const prev = pulseTimers.current.get(id);
    if (prev) clearTimeout(prev);
    pulseTimers.current.set(
      id,
      setTimeout(() => {
        setPulses((p) => {
          const n = new Set(p);
          n.delete(id);
          return n;
        });
        pulseTimers.current.delete(id);
      }, PULSE_MS),
    );
  }, []);

  const flash = useCallback((k: string) => {
    setTendrilsOn((p) => new Set(p).add(k));
    const prev = tendrilTimers.current.get(k);
    if (prev) clearTimeout(prev);
    tendrilTimers.current.set(
      k,
      setTimeout(() => {
        setTendrilsOn((p) => {
          const n = new Set(p);
          n.delete(k);
          return n;
        });
        tendrilTimers.current.delete(k);
      }, PARTICLE_MS),
    );
  }, []);

  const dropParticle = useCallback(
    (id: string) => setParticles((p) => p.filter((x) => x.id !== id)),
    [],
  );

  useImperativeHandle(
    ref,
    () => ({
      emit(sig: Signal) {
        const nid = sig.neuron ?? null;
        let src = SYNAPSE_NODE;
        let dst = SYNAPSE_NODE;
        if (nid && AXON_TYPES.has(sig.type)) {
          src = nid;
          dst = SYNAPSE_NODE;
        } else if (nid) {
          // TARGET_TYPES and everything else addressed at a neuron
          src = SYNAPSE_NODE;
          dst = nid;
          void TARGET_TYPES;
        }
        pulse(SYNAPSE_NODE);
        if (src !== SYNAPSE_NODE) pulse(src);
        if (dst !== SYNAPSE_NODE) pulse(dst);
        if (src !== dst) {
          flash(`${src}::${dst}`);
          setParticles((p) => [
            ...p,
            {
              id: `${sig.id || Math.random()}_${Date.now()}`,
              from: src,
              to: dst,
              color: colorFor(sig.type),
            },
          ]);
        }
      },
    }),
    [pulse, flash],
  );

  const layout = useLayout(neurons, vp);

  const tendrils = useMemo(() => {
    const out: { id: string; from: Point; to: Point; k1: string; k2: string }[] = [];
    for (const ne of neurons.values()) {
      const from = layout[ne.id];
      if (!from) continue;
      out.push({
        id: ne.id,
        from,
        to: layout[SYNAPSE_NODE],
        k1: `${ne.id}::${SYNAPSE_NODE}`,
        k2: `${SYNAPSE_NODE}::${ne.id}`,
      });
    }
    return out;
  }, [neurons, layout]);

  return (
    <svg
      width={vp.w}
      height={vp.h}
      style={{
        position: "absolute",
        top: 64,
        left: 0,
        marginRight: sidebarOffset,
        transition: "margin-right 0.25s ease",
      }}
    >
      <defs>
        <radialGradient id="centerGlow">
          <stop offset="0%" stopColor={C.accent} stopOpacity="0.3" />
          <stop offset="100%" stopColor={C.accent} stopOpacity="0" />
        </radialGradient>
      </defs>
      <circle
        cx={vp.w / 2}
        cy={vp.h / 2}
        r={260}
        fill="url(#centerGlow)"
        style={{ pointerEvents: "none" }}
      />

      {tendrils.map((t) => (
        <Tendril key={t.id} from={t.from} to={t.to} active={tendrilsOn.has(t.k1) || tendrilsOn.has(t.k2)} />
      ))}

      {particles.map((p) => (
        <ParticleDot
          key={p.id}
          id={p.id}
          from={layout[p.from]}
          to={layout[p.to]}
          color={p.color}
          onDone={dropParticle}
        />
      ))}

      <Blob
        x={layout[SYNAPSE_NODE].x}
        y={layout[SYNAPSE_NODE].y}
        r={56}
        color={C.accent}
        pulse={pulses.has(SYNAPSE_NODE)}
        label="synapse"
        sublabel={namespace}
        isSyn
      />

      {Array.from(neurons.values()).map((ne) => {
        const p = layout[ne.id];
        if (!p) return null;
        const color = ne.deregistered ? C.textFaint : colorFor(ne.lastType ?? "REGISTER");
        return (
          <Blob
            key={ne.id}
            x={p.x}
            y={p.y}
            r={28}
            color={color}
            pulse={pulses.has(ne.id)}
            label={ne.id.length > 18 ? ne.id.slice(0, 16) + "…" : ne.id}
            sublabel={ne.capabilities[0] ?? ""}
            onHover={() => onHover(ne.id)}
            onLeave={() => onHover(null)}
          />
        );
      })}

      {neurons.size === 0 && (
        <text
          x={vp.w / 2}
          y={vp.h / 2 + 160}
          textAnchor="middle"
          fill={C.textFaint}
          fontSize="13"
          fontFamily={MONO}
        >
          Waiting for neurons to register…
        </text>
      )}
    </svg>
  );
});

// ── layout ────────────────────────────────────────────────────────────────
function useLayout(neurons: Map<string, NeuronView>, vp: { w: number; h: number }) {
  return useMemo(() => {
    const cx = vp.w / 2;
    const cy = vp.h / 2;
    const baseR = Math.max(180, Math.min(vp.w, vp.h) * 0.32);
    const arr = Array.from(neurons.values());
    const n = Math.max(arr.length, 1);
    const out: Record<string, Point> = {};
    arr.forEach((ne, i) => {
      const a = (i / n) * Math.PI * 2 - Math.PI / 2;
      const ring = Math.floor(i / 12);
      const r = baseR + ring * 70;
      out[ne.id] = { x: cx + Math.cos(a) * r, y: cy + Math.sin(a) * r };
    });
    out[SYNAPSE_NODE] = { x: cx, y: cy };
    return out;
  }, [neurons, vp.w, vp.h]);
}

// ── blob ──────────────────────────────────────────────────────────────────
interface BlobProps {
  x: number;
  y: number;
  r: number;
  color: string;
  pulse: boolean;
  label?: string;
  sublabel?: string;
  isSyn?: boolean;
  onHover?: () => void;
  onLeave?: () => void;
}

function Blob({ x, y, r, color, pulse, label, sublabel, isSyn, onHover, onLeave }: BlobProps) {
  const gid = "g_" + (label || "syn").replace(/[^a-zA-Z0-9]/g, "_");
  return (
    <g
      transform={`translate(${x},${y})`}
      style={{ cursor: "pointer" }}
      onMouseEnter={onHover}
      onMouseLeave={onLeave}
    >
      <defs>
        <radialGradient id={gid}>
          <stop offset="0%" stopColor={color} stopOpacity="0.95" />
          <stop offset="55%" stopColor={color} stopOpacity="0.35" />
          <stop offset="100%" stopColor={color} stopOpacity="0" />
        </radialGradient>
      </defs>
      <circle
        r={r * 2.4}
        fill={`url(#${gid})`}
        style={{
          opacity: pulse ? 0.85 : 0.35,
          transition: "opacity 0.4s ease",
          filter: `blur(${pulse ? "2px" : "4px"})`,
        }}
      />
      {pulse && (
        <circle r={r} fill="none" stroke={color} strokeOpacity="0.7" strokeWidth="2">
          <animate attributeName="r" from={r} to={r * 3.2} dur="1s" repeatCount="1" />
          <animate attributeName="stroke-opacity" from="0.8" to="0" dur="1s" repeatCount="1" />
        </circle>
      )}
      <circle
        r={r}
        fill={C.bgCard}
        stroke={color}
        strokeWidth={isSyn ? 2.5 : 1.5}
        style={{
          filter: `drop-shadow(0 0 ${pulse ? 16 : 8}px ${color})`,
          transition: "filter 0.4s ease",
        }}
      />
      <circle r={r * 0.55} fill={color} fillOpacity="0.18" />
      {isSyn ? (
        <>
          <circle r={r * 0.32} fill="none" stroke={C.accent2} strokeWidth="1.5" strokeOpacity="0.8">
            <animateTransform
              attributeName="transform"
              type="rotate"
              from="0"
              to="360"
              dur="14s"
              repeatCount="indefinite"
            />
          </circle>
          <circle r={r * 0.18} fill={C.accent} fillOpacity="0.7">
            <animate
              attributeName="r"
              values={`${r * 0.18};${r * 0.22};${r * 0.18}`}
              dur="2.4s"
              repeatCount="indefinite"
            />
          </circle>
        </>
      ) : (
        <circle r={r * 0.28} fill={color} fillOpacity="0.85" />
      )}
      {label && (
        <text y={r + 22} textAnchor="middle" fontSize="12" fontWeight="500" fill={C.text} style={{ fontFamily: MONO }}>
          {label}
        </text>
      )}
      {sublabel && (
        <text y={r + 38} textAnchor="middle" fontSize="10" fill={C.textFaint} style={{ fontFamily: MONO }}>
          {sublabel}
        </text>
      )}
    </g>
  );
}

function Tendril({ from, to, active }: { from?: Point; to?: Point; active: boolean }) {
  if (!from || !to) return null;
  const mx = (from.x + to.x) / 2;
  const my = (from.y + to.y) / 2;
  const d = `M ${from.x} ${from.y} Q ${mx} ${my}, ${to.x} ${to.y}`;
  return (
    <path
      d={d}
      fill="none"
      stroke={C.accent}
      strokeOpacity={active ? 0.55 : 0.12}
      strokeWidth={active ? 1.5 : 0.8}
      style={{ transition: "stroke-opacity 0.4s,stroke-width 0.4s" }}
    />
  );
}

function ParticleDot({
  id,
  from,
  to,
  color,
  onDone,
}: {
  id: string;
  from?: Point;
  to?: Point;
  color: string;
  onDone: (id: string) => void;
}) {
  useEffect(() => {
    const t = setTimeout(() => onDone(id), PARTICLE_MS);
    return () => clearTimeout(t);
  }, [id, onDone]);
  if (!from || !to) return null;
  const mx = (from.x + to.x) / 2;
  const my = (from.y + to.y) / 2;
  const path = `M ${from.x} ${from.y} Q ${mx} ${my}, ${to.x} ${to.y}`;
  return (
    <g>
      <circle r="4" fill={color} style={{ filter: `drop-shadow(0 0 6px ${color})` }}>
        <animateMotion dur="1s" repeatCount="1" path={path} fill="freeze" />
        <animate attributeName="r" values="4;6;3" dur="1s" repeatCount="1" />
      </circle>
    </g>
  );
}
