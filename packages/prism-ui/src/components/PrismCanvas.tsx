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
import { AXON_TYPES, SYNAPSE_NODE, TARGET_TYPES, receptorLabel, receptorRef } from "../types";
import type { NeuronView, ParticipantKind, Signal } from "../types";


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
type Particle = { id: string; from: string; via?: string; to: string; color: string };

const PULSE_MS = 800;
const PARTICLE_MS = 1100;
const TWO_LEG_MS = 1800;
const AXON_BUFFER_TTL = 5000; // ms to keep buffered axon signals waiting for a matching target

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
  // trace_id → { neuronId, sigId, expireAt } - buffers axon signals waiting for a target reply
  const axonBuffer = useRef(new Map<string, { neuronId: string; expireAt: number }>());

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
      const nid = sig.directed?.id ?? null;
      const rxid = receptorRef(sig);
      const color = colorFor(sig.type);
      const pid = `${sig.id || Math.random()}_${Date.now()}`;

      // Prune expired buffer entries
      const now = Date.now();
      for (const [k, v] of axonBuffer.current) {
        if (now > v.expireAt) axonBuffer.current.delete(k);
      }

      if (rxid && nid) {
        // A Receptor-authored signal is a two-leg journey by construction:
        // the edge dispatched it, the synapse carried it, a neuron received
        // it. This takes precedence over the AXON/TARGET split below, which
        // only knows about signals that begin inside the brain.
        pulse(rxid);
        pulse(SYNAPSE_NODE);
        pulse(nid);
        flash(`${rxid}::${SYNAPSE_NODE}`);
        flash(`${SYNAPSE_NODE}::${nid}`);
        setParticles((p) => [...p, { id: pid, from: rxid, via: SYNAPSE_NODE, to: nid, color }]);

      } else if (rxid) {
        // Authored by a Receptor but addressed to nobody - still light the
        // edge it came in through.
        pulse(rxid);
        pulse(SYNAPSE_NODE);
        flash(`${rxid}::${SYNAPSE_NODE}`);
        setParticles((p) => [...p, { id: pid, from: rxid, to: SYNAPSE_NODE, color }]);

      } else if (nid && AXON_TYPES.has(sig.type)) {
        // neuron → synapse leg: buffer for potential pairing
        axonBuffer.current.set(sig.trace_id, { neuronId: nid, expireAt: now + AXON_BUFFER_TTL });
        pulse(nid);
        pulse(SYNAPSE_NODE);
        flash(`${nid}::${SYNAPSE_NODE}`);
        setParticles((p) => [...p, { id: pid, from: nid, to: SYNAPSE_NODE, color }]);

      } else if (nid && TARGET_TYPES.has(sig.type)) {
        // synapse → neuron leg: check if we have a buffered source for a two-leg journey
        const buffered = axonBuffer.current.get(sig.trace_id);
        if (buffered && buffered.neuronId !== nid) {
          // Full chain: source neuron → synapse → destination neuron
          axonBuffer.current.delete(sig.trace_id);
          pulse(SYNAPSE_NODE);
          pulse(nid);
          flash(`${SYNAPSE_NODE}::${nid}`);
          setParticles((p) => [...p, { id: pid, from: buffered.neuronId, via: SYNAPSE_NODE, to: nid, color }]);
        } else {
          // No known source - just animate synapse → neuron
          pulse(SYNAPSE_NODE);
          pulse(nid);
          flash(`${SYNAPSE_NODE}::${nid}`);
          setParticles((p) => [...p, { id: pid, from: SYNAPSE_NODE, to: nid, color }]);
        }

      } else {
        // Undirected signal - pulse synapse only
        pulse(SYNAPSE_NODE);
      }
    },
  }), [pulse, flash]);

  const layout = useLayout(neurons, vp);

  const tendrils = useMemo(() => {
    const out: { id: string; from: Point; to: Point; k1: string; k2: string }[] = [];
    for (const ne of neurons.values()) {
      const from = layout[ne.id];
      if (!from) continue;
      out.push({ id: ne.id, from, to: layout[SYNAPSE_NODE], k1: `${ne.id}::${SYNAPSE_NODE}`, k2: `${SYNAPSE_NODE}::${ne.id}` });
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
          <stop offset="100%" stopColor={C.bg} stopOpacity="1" />
        </radialGradient>
        <filter id="blur-sm"><feGaussianBlur stdDeviation="2" /></filter>
        <filter id="blur-md"><feGaussianBlur stdDeviation="5" /></filter>
        <filter id="glow-soft"><feGaussianBlur stdDeviation="3" /></filter>
      </defs>

      {/* Ambient center bloom */}
      <ellipse cx={cx} cy={cy} rx={320} ry={240} fill="url(#centerGlow)" style={{ pointerEvents: "none" }} filter="url(#blur-md)" />

      {/* Axon lines */}
      {tendrils.map((t) => (
        <Tendril key={t.id} from={t.from} to={t.to} active={tendrilsOn.has(t.k1) || tendrilsOn.has(t.k2)} />
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
            kind={ne.kind}
            label={shortLabel(ne)}
            sublabel={
              ne.kind === "engram" ? "engram" :
              ne.kind === "effector" ? "effector" :
              ne.kind === "receptor" ? "receptor" :
              (ne.capabilities[0] ?? "")
            }
            onHover={() => onHover(ne.id)} onLeave={() => onHover(null)}
          />
        );
      })}

      {/* Signal particles - drawn above nodes so the ball of light is
          visible leaving the source and arriving at the destination */}
      {particles.map((p) => (
        <ParticleDot key={p.id} id={p.id} from={layout[p.from]} via={p.via ? layout[p.via] : undefined} to={layout[p.to]} color={p.color} onDone={dropParticle} />
      ))}

      {neurons.size === 0 && (
        <text x={cx} y={cy + 160} textAnchor="middle" fill={C.textFaint} fontSize="15" fontFamily={MONO}>
          Waiting for neurons to register…
        </text>
      )}
    </svg>
  );
});

// The synthetic "rx:" prefix is internal bookkeeping - a user reading the
// canvas should see the receptor's own name, under a "receptor" sublabel that
// already says what it is.
function shortLabel(ne: NeuronView): string {
  const name = receptorLabel(ne.id);
  return name.length > 18 ? name.slice(0, 16) + "…" : name;
}

// ── layout ────────────────────────────────────────────────────────────────
function useLayout(neurons: Map<string, NeuronView>, vp: { w: number; h: number }) {
  return useMemo(() => {
    const cx = vp.w / 2;
    const cy = vp.h / 2;
    const baseR = Math.max(180, Math.min(vp.w, vp.h) * 0.32);
    const all = Array.from(neurons.values());
    // Receptors are the boundary of the brain, so they get their own ring
    // outside every neuron rather than a slot among them. Placing them on
    // the shared ring would put the edge of the system in the middle of it.
    const inner = all.filter((ne) => ne.kind !== "receptor");
    const edge = all.filter((ne) => ne.kind === "receptor");
    const out: Record<string, Point> = {};

    const n = Math.max(inner.length, 1);
    inner.forEach((ne, i) => {
      const a = (i / n) * Math.PI * 2 - Math.PI / 2;
      const ring = Math.floor(i / 12);
      out[ne.id] = { x: cx + Math.cos(a) * (baseR + ring * 70), y: cy + Math.sin(a) * (baseR + ring * 70) };
    });

    const m = Math.max(edge.length, 1);
    const edgeR = baseR + 150;
    edge.forEach((ne, i) => {
      // Offset by half a slot so a lone receptor doesn't sit directly on the
      // radius of the first neuron and hide the axon line behind it.
      const a = ((i + 0.5) / m) * Math.PI * 2 - Math.PI / 2;
      const ring = Math.floor(i / 12);
      out[ne.id] = { x: cx + Math.cos(a) * (edgeR + ring * 70), y: cy + Math.sin(a) * (edgeR + ring * 70) };
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
      {label && <text y={R + 22} textAnchor="middle" fontSize="14.5" fontWeight="600" fill={C.text} style={{ fontFamily: MONO }}>{label}</text>}
      {sublabel && <text y={R + 38} textAnchor="middle" fontSize="13" fill={C.textDim} style={{ fontFamily: MONO }}>{sublabel}</text>}
    </g>
  );
}

// ── neuron / engram / effector / receptor node ──────────────────────────
// kind="neuron"    →  circle   (axon-backed participant - Neurons think)
// kind="engram"    →  diamond  (Engram memory backend - Engrams remember)
// kind="effector"  →  triangle (Effector tool backend - Effectors act)
// kind="receptor"  →  cup      (the listening edge - Receptors listen)
function NeuronNode({ x, y, color, pulse, kind = "neuron", label, sublabel, onHover, onLeave }: {
  x: number; y: number; color: string; pulse: boolean; kind?: ParticipantKind;
  label?: string; sublabel?: string; onHover?: () => void; onLeave?: () => void;
}) {
  const R = 18;
  const glowStr = pulse ? 14 : 7;
  const engramColor = C.engram;
  // Kept in sync with TOOL_CALL/TOOL_RESULT in theme.ts, the same way
  // engramColor matches RECALL/RECALLED - a kind's identity color always
  // matches the color of the traffic it dominates.
  const effectorColor = C.effector;
  const receptorColor = C.receptor;
  const nodeColor =
    kind === "engram" ? engramColor :
    kind === "effector" ? effectorColor :
    kind === "receptor" ? receptorColor :
    color;

  if (kind === "receptor") {
    // A hollow bowl - the annular lower half between r and 0.62r - with the
    // mouth opening away from the synapse. Biologically a receptor *is* a
    // pocket a ligand binds to, and the same outline reads as a dish antenna:
    // the one open silhouette in the set, because a Receptor is the only
    // primitive that faces outward.
    const cup = (r: number) => {
      const i = r * 0.62;
      return `M ${-r},0 A ${r},${r} 0 0 0 ${r},0 L ${i},0 A ${i},${i} 0 0 1 ${-i},0 Z`;
    };
    const wave = (r: number) => `M ${-r},0 A ${r},${r} 0 0 1 ${r},0`;
    return (
      <g transform={`translate(${x},${y})`} style={{ cursor: "pointer" }} onMouseEnter={onHover} onMouseLeave={onLeave}>
        {/* Ambient bloom */}
        <path d={cup(R * 2.6)} fill={nodeColor} fillOpacity={pulse ? 0.16 : 0.06} filter="url(#blur-md)" style={{ transition: "fill-opacity 0.4s" }} />
        {/* Pulse ripple */}
        {pulse && (
          <path d={cup(R * 1.22)} fill="none" stroke={nodeColor} strokeOpacity="0.8" strokeWidth="1.5">
            <animateTransform attributeName="transform" type="scale" from="1" to="3.2" dur="0.9s" repeatCount="1" />
            <animate attributeName="stroke-opacity" from="0.8" to="0" dur="0.9s" repeatCount="1" />
          </path>
        )}
        {/* Reception arcs above the mouth - the receptor's analogue of the
            effector's rotating inner ring: motion that says what it does. */}
        {[1.5, 1.95].map((k, i) => (
          <path key={k} d={wave(R * k)} fill="none" stroke={nodeColor} strokeWidth="1" strokeOpacity={pulse ? 0.5 : 0.28}
            style={{ transition: "stroke-opacity 0.4s" }}>
            <animate attributeName="stroke-opacity" values="0.05;0.4;0.05" dur="2.8s" begin={`${i * 0.5}s`} repeatCount="indefinite" />
          </path>
        ))}
        {/* Outer ring cup */}
        <path d={cup(R * 1.55)} fill="none" stroke={nodeColor} strokeOpacity={pulse ? 0.45 : 0.2} strokeWidth="0.8"
          style={{ transition: "stroke-opacity 0.4s" }} />
        {/* Body */}
        <path d={cup(R * 1.22)} fill={C.bg} stroke={nodeColor} strokeWidth="1.5"
          style={{ filter: `drop-shadow(0 0 ${glowStr}px ${nodeColor})`, transition: "filter 0.4s" }} />
        {/* The bound ligand resting in the pocket */}
        <circle cy={R * 0.52} r={R * 0.26} fill={nodeColor} fillOpacity={pulse ? 0.95 : 0.75} filter="url(#glow-soft)"
          style={{ transition: "fill-opacity 0.3s" }}>
          <animate attributeName="r" values={`${R * 0.2};${R * 0.32};${R * 0.2}`} dur="2.6s" repeatCount="indefinite" />
        </circle>
        {label && <text y={R * 1.22 + 20} textAnchor="middle" fontSize="13.5" fontWeight="500" fill={C.text} style={{ fontFamily: MONO }}>{label}</text>}
        {sublabel && <text y={R * 1.22 + 34} textAnchor="middle" fontSize="12" fill={nodeColor} style={{ fontFamily: MONO }}>{sublabel}</text>}
      </g>
    );
  }

  if (kind === "effector") {
    // Upward-pointing equilateral triangle, inscribed in radius `r`.
    const tri = (r: number) => {
      const dx = r * 0.8660254; // cos(30deg)
      const dy = r * 0.5;       // sin(30deg)
      return `0,${-r} ${dx},${dy} ${-dx},${dy}`;
    };
    return (
      <g transform={`translate(${x},${y})`} style={{ cursor: "pointer" }} onMouseEnter={onHover} onMouseLeave={onLeave}>
        {/* Ambient bloom */}
        <polygon points={tri(R * 2.6)} fill={nodeColor} fillOpacity={pulse ? 0.16 : 0.06} filter="url(#blur-md)" style={{ transition: "fill-opacity 0.4s" }} />
        {/* Pulse ripple */}
        {pulse && (
          <polygon points={tri(R * 1.22)} fill="none" stroke={nodeColor} strokeOpacity="0.8" strokeWidth="1.5">
            <animateTransform attributeName="transform" type="scale" from="1" to="3.2" dur="0.9s" repeatCount="1" />
            <animate attributeName="stroke-opacity" from="0.8" to="0" dur="0.9s" repeatCount="1" />
          </polygon>
        )}
        {/* Outer ring triangle */}
        <polygon points={tri(R * 1.55)} fill="none" stroke={nodeColor} strokeOpacity={pulse ? 0.45 : 0.2} strokeWidth="0.8"
          style={{ transition: "stroke-opacity 0.4s" }} />
        {/* Body */}
        <polygon points={tri(R * 1.22)} fill={C.bg} stroke={nodeColor} strokeWidth="1.5"
          style={{ filter: `drop-shadow(0 0 ${glowStr}px ${nodeColor})`, transition: "filter 0.4s" }} />
        {/* Inner glow fill */}
        <polygon points={tri(R * 0.7)} fill={nodeColor} fillOpacity="0.13" />
        {/* Rotating inner ring (action pulse) */}
        <polygon points={tri(R * 0.44)} fill="none" stroke={nodeColor} strokeWidth="1" strokeOpacity="0.55">
          <animateTransform attributeName="transform" type="rotate" from="0" to="360" dur="6s" repeatCount="indefinite" />
        </polygon>
        {/* Nucleus */}
        <circle cy={R * 0.14} r={R * 0.28} fill={nodeColor} fillOpacity={pulse ? 0.95 : 0.7} filter="url(#glow-soft)"
          style={{ transition: "fill-opacity 0.3s" }}>
          <animate attributeName="r" values={`${R * 0.22};${R * 0.34};${R * 0.22}`} dur="2.8s" repeatCount="indefinite" />
        </circle>
        {label && <text y={R * 1.55 + 18} textAnchor="middle" fontSize="13.5" fontWeight="500" fill={C.text} style={{ fontFamily: MONO }}>{label}</text>}
        {sublabel && <text y={R * 1.55 + 32} textAnchor="middle" fontSize="12" fill={nodeColor} style={{ fontFamily: MONO }}>{sublabel}</text>}
      </g>
    );
  }

  if (kind === "engram") {
    // Diamond (rotated square) for Engram nodes
    const D = R * 1.22; // half-diagonal of the diamond
    const pts = `0,${-D} ${D},0 0,${D} ${-D},0`;
    const ptsOuter = `0,${-D * 2.6} ${D * 2.6},0 0,${D * 2.6} ${-D * 2.6},0`;
    return (
      <g transform={`translate(${x},${y})`} style={{ cursor: "pointer" }} onMouseEnter={onHover} onMouseLeave={onLeave}>
        {/* Ambient bloom */}
        <polygon points={ptsOuter} fill={nodeColor} fillOpacity={pulse ? 0.16 : 0.06} filter="url(#blur-md)" style={{ transition: "fill-opacity 0.4s" }} />
        {/* Pulse ripple */}
        {pulse && (
          <polygon points={pts} fill="none" stroke={nodeColor} strokeOpacity="0.8" strokeWidth="1.5">
            <animateTransform attributeName="transform" type="scale" from="1" to="3.5" dur="0.9s" repeatCount="1" />
            <animate attributeName="stroke-opacity" from="0.8" to="0" dur="0.9s" repeatCount="1" />
          </polygon>
        )}
        {/* Outer ring diamond */}
        <polygon points={`0,${-D * 1.4} ${D * 1.4},0 0,${D * 1.4} ${-D * 1.4},0`}
          fill="none" stroke={nodeColor} strokeOpacity={pulse ? 0.45 : 0.2} strokeWidth="0.8"
          style={{ transition: "stroke-opacity 0.4s" }} />
        {/* Body */}
        <polygon points={pts} fill={C.bg} stroke={nodeColor} strokeWidth="1.5"
          style={{ filter: `drop-shadow(0 0 ${glowStr}px ${nodeColor})`, transition: "filter 0.4s" }} />
        {/* Inner glow fill */}
        <polygon points={`0,${-D * 0.6} ${D * 0.6},0 0,${D * 0.6} ${-D * 0.6},0`}
          fill={nodeColor} fillOpacity="0.13" />
        {/* Rotating inner ring (memory pulse) */}
        <polygon points={`0,${-D * 0.38} ${D * 0.38},0 0,${D * 0.38} ${-D * 0.38},0`}
          fill="none" stroke={nodeColor} strokeWidth="1" strokeOpacity="0.55">
          <animateTransform attributeName="transform" type="rotate" from="45" to="405" dur="8s" repeatCount="indefinite" />
        </polygon>
        {/* Nucleus */}
        <circle r={R * 0.28} fill={nodeColor} fillOpacity={pulse ? 0.95 : 0.7} filter="url(#glow-soft)"
          style={{ transition: "fill-opacity 0.3s" }}>
          <animate attributeName="r" values={`${R * 0.22};${R * 0.34};${R * 0.22}`} dur="3.2s" repeatCount="indefinite" />
        </circle>
        {label && <text y={D + 18} textAnchor="middle" fontSize="13.5" fontWeight="500" fill={C.text} style={{ fontFamily: MONO }}>{label}</text>}
        {sublabel && <text y={D + 32} textAnchor="middle" fontSize="12" fill={nodeColor} style={{ fontFamily: MONO }}>{sublabel}</text>}
      </g>
    );
  }

  // Circle - standard Neuron node
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
      <circle r={R} fill={C.bg} stroke={color} strokeWidth="1.5" style={{ filter: `drop-shadow(0 0 ${glowStr}px ${color})`, transition: "filter 0.4s" }} />
      <circle r={R * 0.6} fill={color} fillOpacity="0.12" />
      <circle r={R * 0.32} fill={C.accent3} fillOpacity={pulse ? 0.95 : 0.75} filter="url(#glow-soft)" style={{ transition: "fill-opacity 0.3s" }}>
        <animate attributeName="r" values={`${R * 0.28};${R * 0.38};${R * 0.28}`} dur="2.4s" repeatCount="indefinite" />
      </circle>
      {label && <text y={R + 18} textAnchor="middle" fontSize="13.5" fontWeight="500" fill={C.text} style={{ fontFamily: MONO }}>{label}</text>}
      {sublabel && <text y={R + 32} textAnchor="middle" fontSize="12" fill={C.textFaint} style={{ fontFamily: MONO }}>{sublabel}</text>}
    </g>
  );
}

// ── axon line ─────────────────────────────────────────────────────────────
function Tendril({ from, to, active }: { from?: Point; to?: Point; active: boolean }) {
  if (!from || !to) return null;
  const mx = (from.x + to.x) / 2;
  const my = (from.y + to.y) / 2;
  const mainD = `M${from.x} ${from.y} Q${mx} ${my},${to.x} ${to.y}`;
  return (
    <g>
      {active && <path d={mainD} fill="none" stroke={C.accent2} strokeOpacity="0.2" strokeWidth="3" filter="url(#blur-sm)" />}
      <path d={mainD} fill="none" stroke={active ? C.accent2 : C.accent} strokeOpacity={active ? 0.6 : 0.12} strokeWidth={active ? 1.2 : 0.6} style={{ transition: "stroke-opacity 0.4s,stroke-width 0.4s,stroke 0.4s" }} />
    </g>
  );
}

// ── signal particle ───────────────────────────────────────────────────────
// A directed ball of light that travels source → destination: a bright
// glowing head with a staggered comet tail trailing behind it along the
// same path, eased so it launches and arrives smoothly.
const TAIL = [
  { delay: 0.07, r: 2.2, opacity: 0.55 },
  { delay: 0.14, r: 1.7, opacity: 0.35 },
  { delay: 0.22, r: 1.2, opacity: 0.18 },
];

function ParticleDot({ id, from, via, to, color, onDone }: {
  id: string; from?: Point; via?: Point; to?: Point; color: string; onDone: (id: string) => void;
}) {
  const twoLeg = !!via;
  const dur = twoLeg ? TWO_LEG_MS : PARTICLE_MS;

  useEffect(() => {
    // +300ms lets the comet tail finish before the particle unmounts.
    const t = setTimeout(() => onDone(id), dur + 300);
    return () => clearTimeout(t);
  }, [id, onDone, dur]);

  if (!from || !to) return null;

  let path: string;
  if (via) {
    // Two-leg path: from → via (synapse) → to
    const mx1 = (from.x + via.x) / 2;
    const my1 = (from.y + via.y) / 2;
    const mx2 = (via.x + to.x) / 2;
    const my2 = (via.y + to.y) / 2;
    path = `M${from.x} ${from.y} Q${mx1} ${my1},${via.x} ${via.y} Q${mx2} ${my2},${to.x} ${to.y}`;
  } else {
    const mx = (from.x + to.x) / 2;
    const my = (from.y + to.y) / 2;
    path = `M${from.x} ${from.y} Q${mx} ${my},${to.x} ${to.y}`;
  }

  const durS = `${(dur / 1000).toFixed(2)}s`;
  // Ease-in-out so the ball visibly launches from the source and settles
  // into the destination. Single-leg journeys ease both ends; two-leg ones
  // stay closer to linear so the synapse fly-through doesn't stall.
  const spline = twoLeg ? "0.35 0 0.65 1" : "0.3 0 0.25 1";
  const motion = (begin?: number) => (
    <animateMotion
      dur={durS}
      begin={begin ? `${begin.toFixed(2)}s` : "0s"}
      repeatCount="1"
      path={path}
      fill="freeze"
      calcMode="spline"
      keyPoints="0;1"
      keyTimes="0;1"
      keySplines={spline}
    />
  );

  return (
    <g style={{ pointerEvents: "none" }}>
      {/* Comet tail - staggered followers tracing the same path */}
      {TAIL.map((t, i) => (
        <circle key={i} r={t.r} fill={color} opacity="0">
          {motion(t.delay)}
          <set attributeName="opacity" to={String(t.opacity)} begin={`${t.delay.toFixed(2)}s`} />
          <animate attributeName="opacity" from={String(t.opacity)} to="0"
            begin={`${(dur / 1000 + t.delay - 0.15).toFixed(2)}s`} dur="0.25s" fill="freeze" />
        </circle>
      ))}
      {/* Halo around the head */}
      <circle r="9" fill={color} fillOpacity="0.18" filter="url(#blur-sm)">
        {motion()}
        <animate attributeName="fill-opacity" values="0;0.22;0.18;0" dur={durS} repeatCount="1" fill="freeze" />
      </circle>
      <circle r="5" fill={color} fillOpacity="0.45" filter="url(#blur-sm)">
        {motion()}
      </circle>
      {/* Bright core */}
      <circle r="2.8" fill={C.spark} style={{ filter: `drop-shadow(0 0 6px ${color}) drop-shadow(0 0 12px ${color})` }}>
        {motion()}
        <animate attributeName="fill" values={`${C.spark};${color};${C.spark}`} dur={durS} repeatCount="1" />
        <animate attributeName="fill-opacity" from="1" to="0"
          begin={`${(dur / 1000 - 0.12).toFixed(2)}s`} dur="0.2s" fill="freeze" />
      </circle>
    </g>
  );
}
