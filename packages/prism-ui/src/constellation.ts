// Constellation — derive a per-run execution graph from the signal buffer and
// score how consistently a setup reproduces that graph across runs.
//
// A run is one top-level task subtree (rootBundles in metrics.ts). Its graph
// has one node per participant that took part (receptor / neuron / engram /
// effector) and one typed edge per interaction channel. The synapse is transport, not a
// participant: when A tool-calls B the envelope crosses the synapse, but the
// edge is A → B. Channels:
//   task     — TASK delegation (dispatching neuron → child neuron), and the
//              entry edge (receptor → the neuron it handed the root task to)
//   tool     — TOOL_CALL → effector          (replies: TOOL_RESULT)
//   recall   — RECALL → engram               (replies: RECALLED)
//   imprint  — IMPRINT → engram              (replies: IMPRINTED)
//   output   — a subtask's FINAL back to the neuron that delegated it
//
// The sender of a request is resolved by LINEAGE, not by trace: every signal
// is attributed to the worker of its nearest TASK ancestor (parent_id chain).
// This is what makes handoff runs read correctly — a capability-routed graph
// keeps every step of a multi-agent run on ONE trace, so "the trace's current
// worker" would credit the whole run to whoever spoke first. Fallbacks, in
// order: the trace's latest TASK target, then the neuron the trace's cognition
// signals (AGENT_OUTPUT / PLAN / THOUGHT_DELTA / FINAL) are attributed to. A
// request whose sender cannot be established contributes node activity but no
// edge.
//
// A TASK need not name its target: capability routing dispatches undirected
// (directed = null, payload.capabilities = [...]) and only the *answer* reveals
// who took it. So a TASK's worker is its directed.id when it has one, else the
// neuron of the first cognition signal that lands inside its scope.
//
// Consistency (structural): runs are grouped into "setups" by their normalized
// task prompt (falling back to the target neuron). Each run reduces to a
// signature — the set of canonical edge strings — and a setup's consistency is
// the mean pairwise Jaccard similarity of its runs' signatures. 100% means the
// same participants wired the same way every run; counts and timing are
// deliberately ignored (structure, not load).

import { rootBundles } from "./metrics";
import { C } from "./theme";
import { ts } from "./grouping";
import { participantKind, receptorRef, type ParticipantKind, type Signal } from "./types";

export type NodeKind = ParticipantKind;

export interface GraphNode {
  id: string;
  kind: NodeKind;
  /** Signals attributed to this node inside the run. */
  activity: number;
  errors: number;
}

export type EdgeChannel = "task" | "tool" | "recall" | "imprint" | "output";

export interface GraphEdge {
  from: string;
  to: string;
  channel: EdgeChannel;
  /** Requests sent along this edge. */
  count: number;
  /** Matched replies (TOOL_RESULT / RECALLED / IMPRINTED). */
  replies: number;
  /** Share of sibling runs (same setup) that also contain this edge. 1 when
   *  the run has no siblings to compare against. */
  freq: number;
}

export interface RunGraph {
  trace: string;
  label: string;
  setupKey: string;
  start: number;
  end: number;
  final: boolean;
  error: boolean;
  nodes: GraphNode[];
  edges: GraphEdge[];
  /** Canonical edge strings — the run's structural signature. */
  signature: Set<string>;
}

export interface SetupGroup {
  key: string;
  label: string;
  /** Most recent first (rootBundles order). */
  runs: RunGraph[];
  /** Mean pairwise Jaccard over run signatures; null when fewer than 2 runs. */
  consistency: number | null;
  /** edge signature → share of runs containing it. */
  edgeFreq: Map<string, number>;
}

export interface ConsistencyReport {
  setups: SetupGroup[];
  runsByTrace: Map<string, RunGraph>;
  /** Pair-weighted mean over setups with ≥2 runs; null when nothing repeats. */
  overall: number | null;
  comparedSetups: number;
  totalRuns: number;
}

export const edgeSig = (e: { from: string; to: string; channel: string }): string =>
  `${e.from}→${e.to}#${e.channel}`;

/** Classify every participant id seen anywhere in the buffer. Registration
 *  wins; otherwise the primitive a signal targets betrays the kind. */
function kindMap(signals: Signal[]): Map<string, ParticipantKind> {
  const kinds = new Map<string, ParticipantKind>();
  for (const s of signals) {
    // Receptors are never addressed, so they are classified by the meta key
    // that names their author rather than by any directed.id.
    const rxid = receptorRef(s);
    if (rxid) kinds.set(rxid, "receptor");
    const id = s.directed?.id;
    if (!id) continue;
    const k = participantKind(s);
    if (k) kinds.set(id, k);
  }
  for (const s of signals) {
    const id = s.directed?.id;
    if (!id || kinds.has(id)) continue;
    if (s.type === "RECALL" || s.type === "IMPRINT") kinds.set(id, "engram");
    else if (s.type === "TOOL_CALL") kinds.set(id, "effector");
    else if (s.type === "TASK") kinds.set(id, "neuron");
  }
  return kinds;
}

const normalize = (s: string): string =>
  s.toLowerCase().replace(/\s+/g, " ").trim().slice(0, 200);

// Cognition signals whose directed.id is the emitting neuron — used to infer
// a trace's worker when it carries no TASK.
const COGNITION = new Set<Signal["type"]>(["AGENT_OUTPUT", "PLAN", "THOUGHT_DELTA", "FINAL"]);

function buildRunGraph(
  sigs: Signal[],
  byId: Map<string, Signal>,
  kinds: Map<string, ParticipantKind>,
  trace: string,
  label: string,
  setupKey: string,
): RunGraph {
  const chron = [...sigs].sort((a, b) => ts(a) - ts(b));
  const nodes = new Map<string, GraphNode>();
  const edges = new Map<string, GraphEdge>();
  const worker = new Map<string, string>(); // trace → current worker neuron
  const dispatcher = new Map<string, string>(); // trace → who delegated its task

  // Fallback worker per trace for TASK-less traces: the first neuron a
  // cognition signal on that trace is attributed to.
  const fallback = new Map<string, string>();
  for (const s of chron) {
    const id = s.directed?.id;
    if (!id || !COGNITION.has(s.type)) continue;
    const trc = s.trace_id || "no-trace";
    if (!fallback.has(trc) && kinds.get(id) !== "engram" && kinds.get(id) !== "effector")
      fallback.set(trc, id);
  }

  // ── scope: the nearest TASK ancestor of a signal ────────────────────────
  // Memoized walk up parent_id. This is the unit of attribution: everything
  // under a TASK was done by whoever ran that TASK, whatever trace it sits on.
  const scope = new Map<string, Signal | null>();
  const enclosingTask = (s: Signal): Signal | null => {
    const memo = scope.get(s.id);
    if (memo !== undefined) return memo;
    const chain: string[] = [];
    let cur: string | null | undefined = s.parent_id;
    let found: Signal | null = null;
    const guard = new Set<string>([s.id]);
    while (cur && !guard.has(cur)) {
      guard.add(cur);
      const p: Signal | undefined = byId.get(cur);
      if (!p) break;
      if (p.type === "TASK") {
        found = p;
        break;
      }
      chain.push(p.id);
      cur = p.parent_id;
    }
    scope.set(s.id, found);
    for (const id of chain) if (!scope.has(id)) scope.set(id, found);
    return found;
  };

  // ── TASK id → the neuron that ran it ────────────────────────────────────
  // Directed dispatch names its target up front. Capability-routed dispatch
  // does not, so the task is credited to the first cognition signal that
  // answers inside its scope.
  const taskWorker = new Map<string, string>();
  for (const s of chron) if (s.type === "TASK" && s.directed?.id) taskWorker.set(s.id, s.directed.id);
  for (const s of chron) {
    const id = s.directed?.id;
    if (!id || !COGNITION.has(s.type)) continue;
    if (kinds.get(id) === "engram" || kinds.get(id) === "effector") continue;
    const t = enclosingTask(s);
    if (t && !taskWorker.has(t.id)) taskWorker.set(t.id, id);
  }

  const node = (id: string): GraphNode => {
    let n = nodes.get(id);
    if (!n) nodes.set(id, (n = { id, kind: kinds.get(id) ?? "neuron", activity: 0, errors: 0 }));
    return n;
  };

  const edge = (from: string, to: string, channel: EdgeChannel): GraphEdge => {
    node(from);
    node(to);
    const key = edgeSig({ from, to, channel });
    let e = edges.get(key);
    if (!e) edges.set(key, (e = { from, to, channel, count: 0, replies: 0, freq: 1 }));
    return e;
  };
  /** Who emitted this signal: the worker of its enclosing TASK, else the
   *  trace's current worker. Null when unknowable. */
  const src = (s: Signal): string | null => {
    const t = enclosingTask(s);
    const byScope = t ? taskWorker.get(t.id) : undefined;
    if (byScope) return byScope;
    const trc = s.trace_id || "no-trace";
    return worker.get(trc) ?? fallback.get(trc) ?? null;
  };

  /** Register a reply on its request's edge. The responder is resolved via
   *  lineage first — reply.parent_id → request.directed.id — because reply
   *  attribution has varied across SDK versions; the reply's own directed.id
   *  is the fallback. */
  const reply = (
    s: Signal,
    from: string | null,
    attributed: string,
    requestType: Signal["type"],
    channel: EdgeChannel,
  ): void => {
    const req = s.parent_id ? byId.get(s.parent_id) : undefined;
    const responder = (req?.type === requestType && req.directed?.id) || attributed;
    if (from && from !== responder) edge(from, responder, channel).replies++;
  };

  let start = Infinity;
  let end = -Infinity;
  let final = false;
  let error = false;

  for (const s of chron) {
    const t = ts(s);
    if (t < start) start = t;
    if (t > end) end = t;
    if (s.type === "FINAL") final = true;
    if (s.type === "ERROR") error = true;

    // Authorship first: a Receptor takes part in the run without ever being
    // addressed, so its activity has to be counted before the directed.id
    // guard that every other participant is found by.
    const rxid = receptorRef(s);
    if (rxid) {
      const rn = node(rxid);
      rn.activity++;
      if (s.type === "ERROR") rn.errors++;
    }

    const trc = s.trace_id || "no-trace";

    // TASK is handled before the directed.id guard: a capability-routed task
    // is dispatched undirected, and its target is only known from the answer.
    if (s.type === "TASK") {
      const target = s.directed?.id ?? taskWorker.get(s.id) ?? null;
      if (target) {
        const tn = node(target);
        tn.activity++;
        // A Receptor-raised task does have a source: the edge it arrived
        // through. Drawing it makes the entry point part of the run's
        // structural signature, so "same work, different door" reads as the
        // structural difference it is.
        if (rxid) edge(rxid, target, "task").count++;

        // Delegation edge: whoever was running the enclosing scope handed
        // this task on. Works for a subtask on its own trace and for a
        // handoff that stays on the parent's trace alike.
        const encl = enclosingTask(s);
        let pw = encl ? taskWorker.get(encl.id) ?? null : null;
        const parent = s.parent_id ? byId.get(s.parent_id) : undefined;
        const pt = parent?.trace_id;
        const crossTrace = !!pt && pt !== trc;
        if (!pw && crossTrace) pw = worker.get(pt!) ?? fallback.get(pt!) ?? null;
        if (pw) {
          // pw === target is a self-delegation loop — kept on purpose: a
          // neuron spawning a task it executes itself is structure.
          edge(pw, target, "task").count++;
          // Only a task that opened its own trace returns its FINAL to the
          // dispatcher; a same-trace handoff already shows as the task edge.
          if (crossTrace) dispatcher.set(trc, pw);
        }
        worker.set(trc, target);
      }
      continue;
    }

    const id = s.directed?.id;
    if (!id) continue;
    const n = node(id);
    n.activity++;
    if (s.type === "ERROR") n.errors++;
    const from = src(s);

    switch (s.type) {
      case "TOOL_CALL":
        if (from && from !== id) edge(from, id, "tool").count++;
        break;
      case "TOOL_RESULT":
        reply(s, from, id, "TOOL_CALL", "tool");
        break;
      case "RECALL":
        if (from && from !== id) edge(from, id, "recall").count++;
        break;
      case "RECALLED":
        reply(s, from, id, "RECALL", "recall");
        break;
      case "IMPRINT":
        if (from && from !== id) edge(from, id, "imprint").count++;
        break;
      case "IMPRINTED":
        reply(s, from, id, "IMPRINT", "imprint");
        break;
      case "FINAL": {
        // A subtask's FINAL flows back to the neuron that delegated it. A
        // root task's FINAL leaves the graph (to the caller), so no edge.
        const dest = dispatcher.get(trc);
        const w = src(s) ?? id;
        if (dest && dest !== w) edge(w, dest, "output").count++;
        break;
      }
      default:
        break;
    }
  }

  if (!isFinite(start)) start = 0;
  if (!isFinite(end)) end = start;

  const edgeList = [...edges.values()];
  return {
    trace,
    label,
    setupKey,
    start,
    end,
    final,
    error,
    nodes: [...nodes.values()],
    edges: edgeList,
    signature: new Set(edgeList.map(edgeSig)),
  };
}

function jaccard(a: Set<string>, b: Set<string>): number {
  if (a.size === 0 && b.size === 0) return 1;
  let inter = 0;
  for (const x of a) if (b.has(x)) inter++;
  const union = a.size + b.size - inter;
  return union ? inter / union : 1;
}

export function computeConsistency(signals: Signal[]): ConsistencyReport {
  const byId = new Map<string, Signal>();
  for (const s of signals) if (s.id) byId.set(s.id, s);
  const kinds = kindMap(signals);

  const bundles = rootBundles(signals);
  const runsByTrace = new Map<string, RunGraph>();
  const setups = new Map<string, SetupGroup>(); // insertion = recency order

  for (const b of bundles) {
    const hint = b.root.hint;
    const target = b.root.taskSig?.directed?.id;
    // Runs of the same setup share a prompt (or at least a target neuron).
    // A run with neither is unique — keyed by its trace, never compared.
    const key = hint ? `hint:${normalize(hint)}` : target ? `target:${target}` : `trace:${b.root.trace}`;
    const label = hint || target || b.root.trace.slice(0, 14);
    const run = buildRunGraph(b.sigs, byId, kinds, b.root.trace, label, key);
    runsByTrace.set(run.trace, run);
    let g = setups.get(key);
    if (!g) setups.set(key, (g = { key, label, runs: [], consistency: null, edgeFreq: new Map() }));
    g.runs.push(run);
  }

  let wSum = 0;
  let wTotal = 0;
  let comparedSetups = 0;
  for (const g of setups.values()) {
    // edge frequency across the setup's runs
    for (const run of g.runs)
      for (const sig of run.signature) g.edgeFreq.set(sig, (g.edgeFreq.get(sig) ?? 0) + 1);
    for (const [sig, n] of g.edgeFreq) g.edgeFreq.set(sig, n / g.runs.length);
    if (g.runs.length > 1)
      for (const run of g.runs)
        for (const e of run.edges) e.freq = g.edgeFreq.get(edgeSig(e)) ?? 1;

    if (g.runs.length < 2) continue;
    comparedSetups++;
    let sum = 0;
    let pairs = 0;
    for (let i = 0; i < g.runs.length; i++)
      for (let j = i + 1; j < g.runs.length; j++) {
        sum += jaccard(g.runs[i].signature, g.runs[j].signature);
        pairs++;
      }
    g.consistency = pairs ? sum / pairs : null;
    if (g.consistency != null) {
      wSum += g.consistency * pairs;
      wTotal += pairs;
    }
  }

  return {
    setups: [...setups.values()],
    runsByTrace,
    overall: wTotal ? wSum / wTotal : null,
    comparedSetups,
    totalRuns: runsByTrace.size,
  };
}

/** Color ramp for consistency badges. */
export function consistencyColor(x: number | null): string {
  if (x == null) return C.textFaint;
  if (x >= 0.9) return C.okSoft;
  if (x >= 0.6) return C.warn;
  return C.danger;
}
