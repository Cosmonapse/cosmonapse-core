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

// ── dendrite branch geometry ──────────────────────────────────────────────
function branchPoints(
  from: { x: number; y: number },
  to: { x: number; y: number },
  seed: number,
): { d: string; opacity: number }[] {
  const branches: { d: string; opacity: number }[] = [];
  const steps = 3;
  let r = seed;
  const rand = () => {
    r = Math.imul(r, 1664525) + 1013904223;
    return (r >>> 0) / 4294967295;
  };
  for (let i = 1; i <= steps; i++) {
    const t = i / (steps + 1);
    const mx = (from.x + to.x) / 2;
    const my = (from.y + to.y) / 2;
    const bx = (1 - t) * (1 - t) * from.x + 2 * (1 - t) * t * mx + t * t * to.x;
    const by = (1 - t) * (1 - t) * from.y + 2 * (1 - t) * t * my + t * t * to.y;
    const dx = to.x - from.x;
    const dy = to.y - from.y;
    const len = Math.sqrt(dx * dx + dy * dy) || 1;
    const nx = -dy / len;
    const ny = dx / len;
    const sign = rand() > 0.5 ? 1 : -1;
    const blen = (22 + rand() * 38) * sign;
    const ex = bx + nx * blen + (rand() - 0.5) * 10;
    const ey = by + ny * blen + (rand() - 0.5) * 10;
    const s1x = ex + (rand() - 0.5) * 24;
    const s1y = ey + (rand() - 0.5) * 24;
    const s2x = ex + (rand() - 0.5) * 18;
    const s2y = ey + (rand() - 0.5) * 18;
    branches.push({ d: `M${bx.toFixed(1)} ${by.toFixed(1)} Q${((bx+ex)/2).toFixed(1)} ${((by+ey)/2).toFixed(1)},${ex.toFixed(1)} ${ey.toFixed(1)}`, opacity: 0.45 + rand() * 0.3 });
    branches.push({ d: `M${ex.toFixed(1)} ${ey.toFixed(1)} L${s1x.toFixed(1)} ${s1y.toFixed(1)}`, opacity: 0.25 + rand() * 0.2 });
    branches.push({ d: `M${ex.toFixed(1)} ${ey.toFixed(1)} L${s2x.toFixed(1)} ${s2y.toFixed(1)}`, opacity: 0.2 + rand() * 0.18 });
  }
  return branches;
}

export interface PrismCanvasHandle {
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
    const resize = () => setVp({ w: window.innerWidth, h: window.innerHeight - 64 });
    window.addEventListener("resize", resize);
    return () => window.removeEventListener("resize", resize);
  }, []);

  const pulse = useCallback((id: string) => {
    setPulses((p) => new Set(p).add(id));
    const prev = pulseTimers.current.get(id);
    if (prev) clearTimeout(prev);
    pulseTimers.current.set(id, setTimeout(() => {
      setPulses((p) => { const n = new Set(p); n.delete(id); return n; });
      pulseTimers.current.delete(id);
    }, PULSE_MS));
  }, []);

  const flash = useCallback((k: string) => {
    setTendrilsOn((p) => new Set(p).add(k));
    const prev = tendrilTimers.current.get(k);
    if (prev) clearTimeout(prev);
    tendrilTimers.current.set(k, setTimeout(() => {
      setTendrilsOn((p) => { const n = new Set(p); n.delete(k); return n; });
      tendrilTimers.current.delete(k);
    }, PARTICLE_MS));
  }, []);

  const dropParticle = useCallback((id: string) => setParticles((p) => p.filter((x) => x.id !== id)), []);

  useImperativeHandle(ref, () => ({
    emit(sig: Signal) {
      const nid = sig.neuron ?? null;
      let src = SYNAPSE_NODE;
      let dst = SYNAPSE_NODE;
      if (nid && AXON_TYPES.has(sig.type)) { src = nid; dst = SYNAPSE_NODE; }
      else if (nid) { src = SYNAPSE_NODE; dst = nid; void TARGET_TYPES; }
      pulse(SYNAPSE_NODE);
      if (src !== SYNAPSE_NODE) pulse(src);
      if (dst !== SYNAPSE_NODE) pulse(dst);
      if (src !== dst) {
        flash(`${src}::${dst}`);
        setParticles((p) => [...p, { id: `${sig.id || Math.random()}_${Date.now()}`, from: src, to: dst, color: colorFor(sig.type) }]);
      }
    },
  }), [pulse, flash]);

  const layout = useLayout(neurons, vp);

  const tendrils = useMemo(() => {
    const out: { id: string; from: Point; to: Point; k1: string; k2: string; seed: number }[] = [];
    for (const ne of neurons.values()) {
      const from = layout[ne.id];
      if (!from) continue;
      let seed = 0;
      for (let i = 0; i < ne.id.length; i++) seed = ((seed << 5) - seed + ne.id.charCodeAt(i)) | 0;
      out.push({ id: ne.id, from, to: layout[SYNAPSE_NODE], k1: `${ne.id}::${SYNAPSE_NODE}`, k2: `${SYNAPSE_NODE}::${ne.id}`, seed });
    }
    return out;
  }, [neurons, layout]);

  const cx = vp.w / 2;
  const cy = vp.h / 2;

  return (
    <svg
      width={vp.w}
      height={vp.h}
      style={{ position: "absolute", top: 64, left: 0, marginRight: sidebarOffset, transition: "margin-right 0.25s ease" }}
    >
      <defs>
        <radialGradient id="centerGlow" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stopColor={C.accent} stopOpacity="0.22" />
          <stop offset="55%" stopColor={C.accent2} stopOpacity="0.08" />
          <stop offset="100%" stopColor={C.accent} stopOpacity="0" />
        </radialGradient>
        <radialGradient id="somaFill" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stopColor={C.accent2} stopOpacity="0.55" />
          <stop offset="40%" stopColor={C.accent} stopOpacity="0.3" />
          <stop offset="100%" stopColor="#07080c" stopOpacity="1" />
        </radialGradient>
        <filter id="blur-sm"><feGaussianBlur stdDeviation="2" /></filter>
        <filter id="blur-md"><feGaussianBlur stdDeviation="5" /></filter>
        <filter id="glow-soft"><feGaussianBlur stdDeviation="3" /></filter>
      </defs>

      {/* Ambient center bloom */}
      <ellipse cx={cx} cy={cy} rx={320} ry={240} fill="url(#centerGlow)" style={{ pointerEvents: "none" }} filter="url(#blur-md)" />

      {/* Dendritic tendrils */}
      {tendrils.map((t) => (
        <Tendril key={t.id} from={t.from} to={t.to} active={tendrilsOn.has(t.k1) || tendrilsOn.has(t.k2)} seed={t.seed} />
      ))}

      {/* Signal particles */}
      {particles.map((p) => (
        <ParticleDot key={p.id} id={p.id} from={layout[p.from]} to={layout[p.to]} color={p.color} onDone={dropParticle} />
      ))}

      {/* Central synapse soma */}
      <SynapseNode x={layout[SYNAPSE_NODE].x} y={layout[SYNAPSE_NODE].y} pulse={pulses.has(SYNAPSE_NODE)} label="synapse" sublabel={namespace} />

      {/* Neuron nodes */}
      {Array.from(neurons.values()).map((ne) => {
        const p = layout[ne.id];
        if (!p) return null;
        const color = ne.deregistered ? C.textFaint : colorFor(ne.lastType ?? "REGISTER");
        return (
          <NeuronNode key={ne.id} x={p.x} y={p.y} color={color} pulse={pulses.has(ne.id)}
            label={ne.id.length > 18 ? ne.id.slice(0, 16) + "…" : ne.id}
            sublabel={ne.capabilities[0] ?? ""}
            onHover={() => onHover(ne.id)} onLeave={() => onHover(null)}
          />
        );
      })}

      {neurons.size === 0 && (
        <text x={cx} y={cy + 160} textAnchor="middle" fill={C.textFaint} fontSize="13" fontFamily={MONO}>
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
      out[ne.id] = { x: cx + Math.cos(a) * (baseR + ring * 70), y: cy + Math.sin(a) * (baseR + ring * 70) };
    });
    out[SYNAPSE_NODE] = { x: cx, y: cy };
    return out;
  }, [neurons, vp.w, vp.h]);
}

// ── central synapse soma ──────────────────────────────────────────────────
function SynapseNode({ x, y, pulse, label, sublabel }: {
  x: number; y: number; pulse: boolean; label: string; sublabel: string;
}) {
  const R = 56;
  const glowStr = pulse ? 22 : 12;
  return (
    <g transform={`translate(${x},${y})`}>
      {/* Outer halo bloom */}
      <circle r={R * 3.2} fill="none" stroke={C.accent} strokeOpacity={pulse ? 0.18 : 0.07} strokeWidth="1" filter="url(#blur-md)" style={{ transition: "stroke-opacity 0.5s" }} />
      <circle r={R * 2.2} fill={C.accent} fillOpacity={pulse ? 0.14 : 0.06} filter="url(#blur-md)" style={{ transition: "fill-opacity 0.4s" }} />
      {/* Ripple on pulse */}
      {pulse && (
        <circle r={R} fill="none" stroke={C.accent2} strokeOpacity="0.7" strokeWidth="2">
          <animate attributeName="r" from={String(R)} to={String(R * 3.4)} dur="1s" repeatCount="1" />
          <animate attributeName="stroke-opacity" from="0.7" to="0" dur="1s" repeatCount="1" />
        </circle>
      )}
      {/* Body */}
      <circle r={R} fill="url(#somaFill)" stroke={C.accent2} strokeWidth="1.5" strokeOpacity="0.7"
        style={{ filter: `drop-shadow(0 0 ${glowStr}px ${C.accent})`, transition: "filter 0.4s" }} />
      {/* Texture rings */}
      <circle r={R * 0.72} fill="none" stroke={C.accent2} strokeWidth="0.8" strokeOpacity="0.22" />
      <circle r={R * 0.48} fill="none" stroke={C.accent} strokeWidth="0.8" strokeOpacity="0.3" />
      {/* Orbiting ring */}
      <circle r={R * 0.34} fill="none" stroke={C.accent2} strokeWidth="1.5" strokeOpacity="0.6">
        <animateTransform attributeName="transform" type="rotate" from="0" to="360" dur="12s" repeatCount="indefinite" />
      </circle>
      {/* Nucleus */}
      <circle r={R * 0.2} fill={C.accent2} fillOpacity="0.85" filter="url(#glow-soft)">
        <animate attributeName="r" values={`${R * 0.18};${R * 0.24};${R * 0.18}`} dur="2.6s" repeatCount="indefinite" />
        <animate attributeName="fill-opacity" values="0.7;0.95;0.7" dur="2.6s" repeatCount="indefinite" />
      </circle>
      {label && <text y={R + 22} textAnchor="middle" fontSize="12" fontWeight="600" fill={C.text} style={{ fontFamily: MONO }}>{label}</text>}
      {sublabel && <text y={R + 38} textAnchor="middle" fontSize="10" fill={C.textDim} style={{ fontFamily: MONO }}>{sublabel}</text>}
    </g>
  );
}

// ── neuron node ───────────────────────────────────────────────────────────
function NeuronNode({ x, y, color, pulse, label, sublabel, onHover, onLeave }: {
  x: number; y: number; color: string; pulse: boolean;
  label?: string; sublabel?: string; onHover?: () => void; onLeave?: () => void;
}) {
  const R = 18;
  const glowStr = pulse ? 14 : 7;
  return (
    <g transform={`translate(${x},${y})`} style={{ cursor: "pointer" }} onMouseEnter={onHover} onMouseLeave={onLeave}>
      <circle r={R * 2.8} fill={color} fillOpacity={pulse ? 0.18 : 0.07} filter="url(#blur-md)" style={{ transition: "fill-opacity 0.4s" }} />
      {pulse && (
        <circle r={R} fill="none" stroke={color} strokeOpacity="0.8" strokeWidth="1.5">
          <animate attributeName="r" from={String(R)} to={String(R * 3.5)} dur="0.9s" repeatCount="1" />
          <animate attributeName="stroke-opacity" from="0.8" to="0" dur="0.9s" repeatCount="1" />
        </circle>
      )}
      <circle r={R * 1.35} fill="none" stroke={color} strokeOpacity={pulse ? 0.45 : 0.2} strokeWidth="0.8" style={{ transition: "stroke-opacity 0.4s" }} />
      <circle r={R} fill="#07080c" stroke={color} strokeWidth="1.5" style={{ filter: `drop-shadow(0 0 ${glowStr}px ${color})`, transition: "filter 0.4s" }} />
      <circle r={R * 0.6} fill={color} fillOpacity="0.12" />
      <circle r={R * 0.32} fill={C.accent3} fillOpacity={pulse ? 0.95 : 0.75} filter="url(#glow-soft)" style={{ transition: "fill-opacity 0.3s" }}>
        <animate attributeName="r" values={`${R * 0.28};${R * 0.38};${R * 0.28}`} dur="2.4s" repeatCount="indefinite" />
      </circle>
      {label && <text y={R + 18} textAnchor="middle" fontSize="11" fontWeight="500" fill={C.text} style={{ fontFamily: MONO }}>{label}</text>}
      {sublabel && <text y={R + 32} textAnchor="middle" fontSize="9" fill={C.textFaint} style={{ fontFamily: MONO }}>{sublabel}</text>}
    </g>
  );
}

// ── dendritic tendril ─────────────────────────────────────────────────────
function Tendril({ from, to, active, seed }: { from?: Point; to?: Point; active: boolean; seed: number }) {
  if (!from || !to) return null;
  const mx = (from.x + to.x) / 2;
  const my = (from.y + to.y) / 2;
  const mainD = `M${from.x} ${from.y} Q${mx} ${my},${to.x} ${to.y}`;
  const branches = branchPoints(from, to, seed);
  const axonColor = active ? C.accent2 : C.accent;
  return (
    <g>
      {active && <path d={mainD} fill="none" stroke={C.accent2} strokeOpacity="0.22" strokeWidth="4" filter="url(#blur-sm)" />}
      <path d={mainD} fill="none" stroke={axonColor} strokeOpacity={active ? 0.75 : 0.18} strokeWidth={active ? 1.8 : 1} style={{ transition: "stroke-opacity 0.4s,stroke-width 0.4s,stroke 0.4s" }} />
      {branches.map((b, i) => (
        <path key={i} d={b.d} fill="none" stroke={axonColor} strokeOpacity={active ? b.opacity * 0.9 : b.opacity * 0.35} strokeWidth={active ? 0.9 : 0.5} style={{ transition: "stroke-opacity 0.4s,stroke 0.4s" }} />
      ))}
    </g>
  );
}

// ── signal particle ───────────────────────────────────────────────────────
function ParticleDot({ id, from, to, color, onDone }: { id: string; from?: Point; to?: Point; color: string; onDone: (id: string) => void }) {
  useEffect(() => {
    const t = setTimeout(() => onDone(id), PARTICLE_MS);
    return () => clearTimeout(t);
  }, [id, onDone]);
  if (!from || !to) return null;
  const mx = (from.x + to.x) / 2;
  const my = (from.y + to.y) / 2;
  const path = `M${from.x} ${from.y} Q${mx} ${my},${to.x} ${to.y}`;
  return (
    <g>
      <circle r="7" fill={color} fillOpacity="0.3" filter="url(#blur-sm)">
        <animateMotion dur="1.1s" repeatCount="1" path={path} fill="freeze" />
      </circle>
      <circle r="4" fill={color} style={{ filter: `drop-shadow(0 0 8px ${color})` }}>
        <animateMotion dur="1.1s" repeatCount="1" path={path} fill="freeze" />
        <animate attributeName="r" values="3;5.5;3" dur="1.1s" repeatCount="1" />
        <animate attributeName="fill-opacity" values="0.9;1;0.6" dur="1.1s" repeatCount="1" />
      </circle>
    </g>
  );
}
