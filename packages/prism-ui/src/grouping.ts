// Signal grouping — turn the flat rolling buffer into a task tree.
//
// Shape:  task (trace) → pathway → signals, and now task → child tasks.
//
// A pathway is the SDK's request/reply unit inside one trace: signals with no
// request lineage sit on the main task pathway; a request (RECALL, IMPRINT,
// CLARIFICATION, …) opens a sub-pathway that collects its replies
// (parent_id = request id).
//
// Nesting: a TASK dispatched from *inside* a running task carries
// parent_id = the parent task's TASK id (stamped by Dendrite.dispatch from the
// ambient trace context). That parent id lives in the parent's trace, so we
// resolve child-trace → parent-trace through it and hang the child task under
// its parent. Tasks whose parent is absent from the buffer (or null) are roots.

import type { Signal, SignalType } from "./types";

// Traces made up purely of these types are housekeeping, not tasks - they get
// folded into one "lifecycle" bucket so the grouped view stays readable.
const LIFECYCLE = new Set<SignalType>(["REGISTER", "DEREGISTER", "HEARTBEAT", "DISCOVER"]);

// Signal types that OPEN a request pathway. Their replies carry
// parent_id = opener.id, so opener + replies group together.
export const REQUEST_TYPES = new Set<SignalType>([
  "RECALL",
  "IMPRINT",
  "CLARIFICATION",
  "PERMISSION",
  "TASK_OFFER",
]);

// Engram / memory traffic. A trace made ONLY of these (no TASK) is an orphan
// side-effect - e.g. an imprint dispatched from a detector hook gets a fresh
// trace_id - and is stitched into the task that was in flight at that moment.
const ENGRAM_TYPES = new Set<SignalType>([
  "RECALL",
  "RECALLED",
  "IMPRINT",
  "IMPRINTED",
  "MEMORY_APPEND",
  "CONTEXT_SYNC",
]);

// Max age gap (ms) allowed when stitching an orphan engram trace to a task.
const STITCH_WINDOW_MS = 60_000;

export const MAIN_PATHWAY = "__main__";

export interface PathwayGroup {
  key: string;
  label: string;
  /** True when the group was linked by time, not by trace/parent ids. */
  approx: boolean;
  signals: Signal[];
}

export interface TaskGroup {
  trace: string;
  taskSig?: Signal;
  hint?: string;
  /** Signals on this task's own trace. */
  count: number;
  /** Signals on this task plus every descendant task. */
  subtreeCount: number;
  /** 0 for roots, +1 per nesting level. */
  depth: number;
  pathways: PathwayGroup[];
  children: TaskGroup[];
}

export interface Grouped {
  /** Top-level tasks; each may carry nested `children`. */
  roots: TaskGroup[];
  lifecycle: Signal[];
}

/** Pull a human hint for a task group out of its TASK signal payload. */
export function taskHint(sig: Signal | undefined): string | undefined {
  if (!sig?.payload) return undefined;
  for (const k of ["input", "prompt", "task", "description", "goal"]) {
    const v = sig.payload[k];
    if (typeof v === "string" && v.trim()) return v.trim();
    if (v && typeof v === "object") {
      // common shape: payload.input.prompt
      for (const kk of ["prompt", "task", "text", "query"]) {
        const vv = (v as Record<string, unknown>)[kk];
        if (typeof vv === "string" && vv.trim()) return vv.trim();
      }
    }
  }
  return undefined;
}

export const ts = (s: Signal) => new Date(s.ts).getTime() || 0;

export function groupSignals(signals: Signal[]): Grouped {
  // signals arrive newest-first; iterate in that order so task groups are
  // ordered by most recent activity.
  const byTrace = new Map<string, Signal[]>();
  const byId = new Map<string, Signal>();
  for (const sig of signals) {
    const trace = sig.trace_id || "no-trace";
    let arr = byTrace.get(trace);
    if (!arr) byTrace.set(trace, (arr = []));
    arr.push(sig);
    if (sig.id) byId.set(sig.id, sig);
  }

  const lifecycle: Signal[] = [];
  const taskTraces = new Map<string, Signal[]>(); // insertion = recency order
  const orphans: { trace: string; sigs: Signal[] }[] = [];

  for (const [trace, sigs] of byTrace) {
    if (sigs.every((s) => LIFECYCLE.has(s.type))) {
      lifecycle.push(...sigs);
    } else if (!sigs.some((s) => s.type === "TASK") && sigs.every((s) => ENGRAM_TYPES.has(s.type))) {
      orphans.push({ trace, sigs });
    } else {
      taskTraces.set(trace, sigs);
    }
  }

  // ── stitch orphan engram traces into the task in flight at that time ──
  const stitched = new Set<string>(); // signal ids that were stitched in
  for (const o of orphans) {
    const oldest = o.sigs[o.sigs.length - 1];
    const start = signals.indexOf(oldest);
    let owner: string | null = null;
    for (let i = start + 1; i < signals.length; i++) {
      const cand = signals[i];
      const candTrace = cand.trace_id || "no-trace";
      if (!taskTraces.has(candTrace)) continue;
      if (ts(oldest) - ts(cand) > STITCH_WINDOW_MS) break;
      owner = candTrace;
      break;
    }
    if (owner) {
      const merged = [...taskTraces.get(owner)!, ...o.sigs];
      merged.sort((a, b) => ts(b) - ts(a)); // keep newest-first invariant
      taskTraces.set(owner, merged);
      for (const s of o.sigs) stitched.add(s.id);
    } else {
      taskTraces.set(o.trace, o.sigs); // no candidate - keep as its own group
    }
  }

  // ── build a (flat) TaskGroup per task trace ──
  const groups = new Map<string, TaskGroup>(); // trace → group, recency order
  for (const [trace, sigs] of taskTraces) {
    const pathways = new Map<string, Signal[]>();
    for (const sig of sigs) {
      let key = MAIN_PATHWAY;
      if (REQUEST_TYPES.has(sig.type)) {
        key = sig.id || MAIN_PATHWAY; // request opens its own pathway
      } else if (sig.parent_id) {
        const parent = byId.get(sig.parent_id);
        if (!parent || REQUEST_TYPES.has(parent.type)) key = sig.parent_id;
      }
      let arr = pathways.get(key);
      if (!arr) pathways.set(key, (arr = []));
      arr.push(sig);
    }

    const pgroups: PathwayGroup[] = [];
    for (const [key, arr] of pathways) {
      arr.sort((a, b) => ts(a) - ts(b)); // chronological within a pathway
      if (key === MAIN_PATHWAY) {
        pgroups.push({ key, label: "task pathway", approx: false, signals: arr });
        continue;
      }
      const opener = byId.get(key);
      const approx = arr.some((s) => stitched.has(s.id));
      const label = opener
        ? `↳ ${opener.type}${opener.directed?.id ? " · " + opener.directed.id : ""}`
        : `↳ thread ${key.slice(4, 12)}`;
      pgroups.push({ key, label, approx, signals: arr });
    }
    pgroups.sort((a, b) => {
      if (a.key === MAIN_PATHWAY) return -1;
      if (b.key === MAIN_PATHWAY) return 1;
      return ts(a.signals[0]) - ts(b.signals[0]);
    });

    const taskSig = [...sigs].reverse().find((s) => s.type === "TASK");
    groups.set(trace, {
      trace,
      ...(taskSig ? { taskSig } : {}),
      ...(taskHint(taskSig) ? { hint: taskHint(taskSig) } : {}),
      count: sigs.length,
      subtreeCount: sigs.length,
      depth: 0,
      pathways: pgroups,
      children: [],
    });
  }

  // ── link children to parents via the TASK signal's parent_id ──
  // parentTrace[trace] = the trace that owns this task's parent signal, if it
  // is itself a task trace and not the task itself.
  const parentTrace = new Map<string, string>();
  for (const [trace, g] of groups) {
    const pid = g.taskSig?.parent_id;
    if (!pid) continue;
    const parentSig = byId.get(pid);
    const pt = parentSig?.trace_id;
    if (pt && pt !== trace && groups.has(pt)) parentTrace.set(trace, pt);
  }

  // Break any accidental cycles: walking up must terminate. If a chain revisits
  // a node, sever the offending link so that node becomes a root.
  for (const trace of groups.keys()) {
    const seen = new Set<string>([trace]);
    let cur = parentTrace.get(trace);
    while (cur) {
      if (seen.has(cur)) {
        parentTrace.delete(cur);
        break;
      }
      seen.add(cur);
      cur = parentTrace.get(cur);
    }
  }

  // Attach children (recency order preserved from `groups` insertion).
  const roots: TaskGroup[] = [];
  for (const [trace, g] of groups) {
    const pt = parentTrace.get(trace);
    if (pt && groups.has(pt)) groups.get(pt)!.children.push(g);
    else roots.push(g);
  }

  // Depth + subtree counts via DFS from roots.
  const setDepth = (g: TaskGroup, depth: number): number => {
    g.depth = depth;
    let total = g.count;
    for (const c of g.children) total += setDepth(c, depth + 1);
    g.subtreeCount = total;
    return total;
  };
  for (const r of roots) setDepth(r, 0);

  return { roots, lifecycle };
}
