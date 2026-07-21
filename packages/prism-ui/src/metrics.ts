// Metrics — derive timing insight from the rolling signal buffer.
//
// Everything here is computed purely from signal timestamps and lineage in the
// buffer; nothing extra is emitted by the SDK. Three views:
//   • time per task     — TASK.ts → last signal on that trace (or FINAL)
//   • tool-call latency — TOOL_CALL → matching TOOL_RESULT
//   • memory retrieval  — RECALL   → matching RECALLED
//
// Pairing prefers explicit lineage (reply.parent_id === request.id) and falls
// back to nearest-earlier unmatched request on the same trace.

import { groupSignals, taskHint, ts, type TaskGroup } from "./grouping";
import { participantKind, type Signal } from "./types";

export interface TaskTiming {
  trace: string;
  label: string;
  start: number;
  end: number;
  durationMs: number;
  final: boolean; // reached FINAL (vs. still-open / errored)
}

export interface PairTiming {
  id: string;
  label: string;
  trace: string;
  durationMs: number;
}

export interface DurStat {
  count: number;
  totalMs: number;
  avgMs: number;
  maxMs: number;
}

export interface Metrics {
  tasks: TaskTiming[];
  taskAgg: DurStat;
  toolCalls: PairTiming[];
  toolAgg: DurStat;
  recalls: PairTiming[];
  recallAgg: DurStat;
}

function agg(durations: number[]): DurStat {
  const count = durations.length;
  const totalMs = durations.reduce((a, b) => a + b, 0);
  return {
    count,
    totalMs,
    avgMs: count ? totalMs / count : 0,
    maxMs: count ? Math.max(...durations) : 0,
  };
}

/** Pair request signals with their replies. Prefers reply.parent_id ===
 *  request.id; otherwise the nearest earlier unmatched request on the same
 *  trace. Returns one PairTiming per matched reply. */
function pairRequests(
  signals: Signal[],
  requestType: Signal["type"],
  replyType: Signal["type"],
): PairTiming[] {
  // chronological so "nearest earlier" is well defined
  const chron = [...signals].sort((a, b) => ts(a) - ts(b));
  const requestsById = new Map<string, Signal>();
  const openByTrace = new Map<string, Signal[]>(); // unmatched requests per trace
  const matched = new Set<string>();
  const out: PairTiming[] = [];

  for (const s of chron) {
    if (s.type === requestType) {
      if (s.id) requestsById.set(s.id, s);
      const arr = openByTrace.get(s.trace_id) ?? [];
      arr.push(s);
      openByTrace.set(s.trace_id, arr);
    } else if (s.type === replyType) {
      let req: Signal | undefined;
      if (s.parent_id && requestsById.has(s.parent_id) && !matched.has(s.parent_id)) {
        req = requestsById.get(s.parent_id);
      } else {
        // nearest earlier unmatched request on the same trace
        const arr = openByTrace.get(s.trace_id);
        if (arr) {
          for (let i = arr.length - 1; i >= 0; i--) {
            if (!matched.has(arr[i].id)) {
              req = arr[i];
              break;
            }
          }
        }
      }
      if (!req) continue;
      matched.add(req.id);
      const target = s.directed?.id || req.directed?.id || "";
      out.push({
        id: s.id || req.id,
        label: target || (req.trace_id || "").slice(4, 12),
        trace: req.trace_id,
        durationMs: Math.max(0, ts(s) - ts(req)),
      });
    }
  }
  return out;
}

export function computeMetrics(signals: Signal[]): Metrics {
  // ── per-task durations ──
  const byTrace = new Map<string, Signal[]>();
  for (const s of signals) {
    const arr = byTrace.get(s.trace_id) ?? [];
    arr.push(s);
    byTrace.set(s.trace_id, arr);
  }
  const tasks: TaskTiming[] = [];
  for (const [trace, sigs] of byTrace) {
    const taskSig = sigs.find((s) => s.type === "TASK");
    if (!taskSig) continue;
    const start = ts(taskSig);
    let end = start;
    let final = false;
    for (const s of sigs) {
      const t = ts(s);
      if (t > end) end = t;
      if (s.type === "FINAL") final = true;
    }
    tasks.push({
      trace,
      label: taskHint(taskSig) || (taskSig.directed?.id ?? trace.slice(4, 12)),
      start,
      end,
      durationMs: Math.max(0, end - start),
      final,
    });
  }
  tasks.sort((a, b) => b.end - a.end); // most recent first

  const toolCalls = pairRequests(signals, "TOOL_CALL", "TOOL_RESULT")
    .sort((a, b) => b.durationMs - a.durationMs);
  const recalls = pairRequests(signals, "RECALL", "RECALLED")
    .sort((a, b) => b.durationMs - a.durationMs);

  return {
    tasks,
    taskAgg: agg(tasks.map((t) => t.durationMs)),
    toolCalls,
    toolAgg: agg(toolCalls.map((t) => t.durationMs)),
    recalls,
    recallAgg: agg(recalls.map((t) => t.durationMs)),
  };
}

/** Human-friendly ms → "12ms" / "1.4s" / "2m 03s". */
export function fmtMs(ms: number): string {
  if (!isFinite(ms) || ms < 0) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(ms < 10_000 ? 2 : 1)}s`;
  const m = Math.floor(ms / 60_000);
  const s = Math.round((ms % 60_000) / 1000);
  return `${m}m ${String(s).padStart(2, "0")}s`;
}

// ── shared: roll each top-level task up over its whole subtree ──────────────
export interface RootBundle {
  root: TaskGroup;
  traces: Set<string>;
  sigs: Signal[];
}
export function rootBundles(signals: Signal[]): RootBundle[] {
  const { roots } = groupSignals(signals);
  const byTrace = new Map<string, Signal[]>();
  for (const s of signals) {
    const a = byTrace.get(s.trace_id) ?? [];
    a.push(s);
    byTrace.set(s.trace_id, a);
  }
  const collect = (t: TaskGroup, set: Set<string>) => {
    set.add(t.trace);
    t.children.forEach((c) => collect(c, set));
  };
  return roots.map((root) => {
    const traces = new Set<string>();
    collect(root, traces);
    const sigs: Signal[] = [];
    for (const tr of traces) sigs.push(...(byTrace.get(tr) ?? []));
    return { root, traces, sigs };
  });
}

const attempt = (s: Signal): number =>
  typeof s.meta?.attempt === "number" ? (s.meta.attempt as number) : 0;

// ── per-task rollup + wall-clock composition ────────────────────────────────
export interface TaskMetric {
  trace: string;
  label: string;
  durationMs: number;
  final: boolean;
  error: boolean;
  toolCount: number;
  toolMs: number;
  recallCount: number;
  recallMs: number;
  writeMs: number;
  waitMs: number; // blocked on clarification / permission
  otherMs: number; // duration minus the measured buckets (compute / thinking)
  ttfoMs: number | null; // time to first AGENT_OUTPUT
  retries: number;
  escalations: number;
  subtasks: number;
}

export function perTaskMetrics(signals: Signal[]): TaskMetric[] {
  const bundles = rootBundles(signals);
  const tools = pairRequests(signals, "TOOL_CALL", "TOOL_RESULT");
  const recalls = pairRequests(signals, "RECALL", "RECALLED");
  const writes = pairRequests(signals, "IMPRINT", "IMPRINTED");
  const clar = pairRequests(signals, "CLARIFICATION", "CLARIFICATION_ANSWER");
  const perm = pairRequests(signals, "PERMISSION", "PERMISSION_DECISION");
  const sumIn = (ps: PairTiming[], t: Set<string>) =>
    ps.filter((p) => t.has(p.trace)).reduce((a, b) => a + b.durationMs, 0);
  const cntIn = (ps: PairTiming[], t: Set<string>) => ps.filter((p) => t.has(p.trace)).length;
  const countSubtasks = (t: TaskGroup): number =>
    t.children.reduce((n, c) => n + 1 + countSubtasks(c), 0);

  const out = bundles.map((b) => {
    let start = b.root.taskSig ? ts(b.root.taskSig) : Infinity;
    let end = -Infinity;
    let final = false;
    let error = false;
    for (const s of b.sigs) {
      const t = ts(s);
      if (t > end) end = t;
      if (s.type === "FINAL") final = true;
      if (s.type === "ERROR") error = true;
    }
    if (!isFinite(start)) start = isFinite(end) ? end : 0;
    const durationMs = Math.max(0, end - start);
    const toolMs = sumIn(tools, b.traces);
    const recallMs = sumIn(recalls, b.traces);
    const writeMs = sumIn(writes, b.traces);
    const waitMs = sumIn(clar, b.traces) + sumIn(perm, b.traces);
    const otherMs = Math.max(0, durationMs - toolMs - recallMs - writeMs - waitMs);
    const outs = b.sigs.filter((s) => s.type === "AGENT_OUTPUT").map(ts);
    const ttfoMs = outs.length ? Math.max(0, Math.min(...outs) - start) : null;
    return {
      trace: b.root.trace,
      label: b.root.hint || b.root.taskSig?.directed?.id || b.root.trace.slice(0, 14),
      durationMs,
      final,
      error,
      toolCount: cntIn(tools, b.traces),
      toolMs,
      recallCount: cntIn(recalls, b.traces),
      recallMs,
      writeMs,
      waitMs,
      otherMs,
      ttfoMs,
      retries: b.sigs.filter((s) => s.type === "TASK" && attempt(s) > 0).length,
      escalations: b.sigs.filter((s) => s.type === "ESCALATION").length,
      subtasks: countSubtasks(b.root),
    };
  });
  out.sort((a, b) => b.durationMs - a.durationMs);
  return out;
}

// ── health ──────────────────────────────────────────────────────────────────
export interface HealthMetrics {
  tasks: number;
  completed: number;
  failed: number;
  inFlight: number;
  successRate: number; // over decided tasks
  retries: number;
  escalations: number;
}
export function computeHealth(signals: Signal[]): HealthMetrics {
  const bundles = rootBundles(signals);
  let completed = 0;
  let failed = 0;
  let inFlight = 0;
  for (const b of bundles) {
    const hasFinal = b.sigs.some((s) => s.type === "FINAL");
    const hasError = b.sigs.some((s) => s.type === "ERROR");
    if (hasFinal) completed++;
    else if (hasError) failed++;
    else inFlight++;
  }
  const decided = completed + failed;
  return {
    tasks: bundles.length,
    completed,
    failed,
    inFlight,
    successRate: decided ? completed / decided : 0,
    retries: signals.filter((s) => s.type === "TASK" && attempt(s) > 0).length,
    escalations: signals.filter((s) => s.type === "ESCALATION").length,
  };
}

// ── responsiveness ──────────────────────────────────────────────────────────
export interface Responsiveness {
  firstOutput: DurStat;
  plan: DurStat;
}
export function computeResponsiveness(signals: Signal[]): Responsiveness {
  const bundles = rootBundles(signals);
  const fo: number[] = [];
  const pl: number[] = [];
  for (const b of bundles) {
    const start = b.root.taskSig ? ts(b.root.taskSig) : Math.min(...b.sigs.map(ts));
    const outs = b.sigs.filter((s) => s.type === "AGENT_OUTPUT").map(ts);
    if (outs.length) fo.push(Math.max(0, Math.min(...outs) - start));
    const plans = b.sigs.filter((s) => s.type === "PLAN").map(ts);
    if (plans.length) pl.push(Math.max(0, Math.min(...plans) - start));
  }
  return { firstOutput: agg(fo), plan: agg(pl) };
}

// ── human-in-the-loop ───────────────────────────────────────────────────────
export interface HitlMetrics {
  clarifyAgg: DurStat;
  clarifyTasks: number;
  permAgg: DurStat;
  approvals: number;
  denials: number;
}
export function computeHitl(signals: Signal[]): HitlMetrics {
  const clar = pairRequests(signals, "CLARIFICATION", "CLARIFICATION_ANSWER");
  const perm = pairRequests(signals, "PERMISSION", "PERMISSION_DECISION");
  let approvals = 0;
  let denials = 0;
  for (const s of signals)
    if (s.type === "PERMISSION_DECISION") {
      if (s.payload?.granted === true) approvals++;
      else denials++;
    }
  return {
    clarifyAgg: agg(clar.map((x) => x.durationMs)),
    clarifyTasks: new Set(signals.filter((s) => s.type === "CLARIFICATION").map((s) => s.trace_id)).size,
    permAgg: agg(perm.map((x) => x.durationMs)),
    approvals,
    denials,
  };
}

// ── memory effectiveness ────────────────────────────────────────────────────
export interface MemoryMetrics {
  recallAgg: DurStat;
  recallCount: number;
  hitRate: number | null; // over RECALLED that carried a hits array
  hitsSampled: number;
  writeAgg: DurStat;
  writeCount: number;
  writeErrors: number;
  reads: number;
  writes: number;
}
export function computeMemory(signals: Signal[]): MemoryMetrics {
  const recall = pairRequests(signals, "RECALL", "RECALLED");
  const write = pairRequests(signals, "IMPRINT", "IMPRINTED");
  let hitsSampled = 0;
  let hits = 0;
  let writeErrors = 0;
  for (const s of signals) {
    if (s.type === "RECALLED") {
      const h = s.payload?.hits;
      if (Array.isArray(h)) {
        hitsSampled++;
        if (h.length > 0) hits++;
      }
    }
    if (s.type === "IMPRINTED") {
      const e = s.payload?.error;
      if (e != null && e !== false) writeErrors++;
    }
  }
  return {
    recallAgg: agg(recall.map((x) => x.durationMs)),
    recallCount: recall.length,
    hitRate: hitsSampled ? hits / hitsSampled : null,
    hitsSampled,
    writeAgg: agg(write.map((x) => x.durationMs)),
    writeCount: write.length,
    writeErrors,
    reads: signals.filter((s) => s.type === "RECALL").length,
    writes: signals.filter((s) => s.type === "IMPRINT").length,
  };
}

// ── per-participant activity ────────────────────────────────────────────────
export interface ParticipantMetric {
  id: string;
  kind: string;
  total: number;
  tasks: number;
  outputs: number;
  errors: number;
  errorRate: number;
  lastSeen: string;
  capabilities: string[];
}
export function computeParticipants(signals: Signal[]): ParticipantMetric[] {
  const map = new Map<string, ParticipantMetric>();
  for (const s of signals) {
    const id = s.directed?.id;
    if (!id) continue;
    let p = map.get(id);
    if (!p) {
      p = { id, kind: "—", total: 0, tasks: 0, outputs: 0, errors: 0, errorRate: 0, lastSeen: s.ts, capabilities: [] };
      map.set(id, p);
    }
    p.total++;
    if (new Date(s.ts).getTime() > new Date(p.lastSeen).getTime()) p.lastSeen = s.ts;
    if (s.type === "TASK") p.tasks++;
    else if (s.type === "AGENT_OUTPUT" || s.type === "FINAL") p.outputs++;
    else if (s.type === "ERROR") p.errors++;
    const k = participantKind(s);
    if (k) p.kind = k;
    if (s.type === "REGISTER") {
      const caps = s.payload?.capabilities;
      if (Array.isArray(caps)) p.capabilities = caps as string[];
    }
  }
  const out = [...map.values()];
  for (const p of out) p.errorRate = p.outputs + p.errors ? p.errors / (p.outputs + p.errors) : 0;
  out.sort((a, b) => b.total - a.total);
  return out;
}

// ── market / coordination ───────────────────────────────────────────────────
export interface MarketMetrics {
  offers: number;
  awarded: number;
  awardAgg: DurStat;
  bids: number;
  bidsPerOffer: number;
  declined: number;
  declineRate: number;
  wins: { id: string; count: number }[];
}
export function computeMarket(signals: Signal[]): MarketMetrics {
  const offers = signals.filter((s) => s.type === "TASK_OFFER").length;
  const awardedSigs = signals.filter((s) => s.type === "TASK_AWARDED");
  const bids = signals.filter((s) => s.type === "BID").length;
  const declined = signals.filter((s) => s.type === "TASK_DECLINED").length;
  const byTrace = new Map<string, { offer?: number; award?: number }>();
  for (const s of signals) {
    if (s.type === "TASK_OFFER") {
      const e = byTrace.get(s.trace_id) ?? {};
      e.offer = Math.min(e.offer ?? Infinity, ts(s));
      byTrace.set(s.trace_id, e);
    } else if (s.type === "TASK_AWARDED") {
      const e = byTrace.get(s.trace_id) ?? {};
      e.award = Math.max(e.award ?? -Infinity, ts(s));
      byTrace.set(s.trace_id, e);
    }
  }
  const awardDur: number[] = [];
  for (const e of byTrace.values()) if (e.offer != null && e.award != null) awardDur.push(Math.max(0, e.award - e.offer));
  const wins = new Map<string, number>();
  for (const s of awardedSigs) {
    const id = s.directed?.id;
    if (id) wins.set(id, (wins.get(id) ?? 0) + 1);
  }
  return {
    offers,
    awarded: awardedSigs.length,
    awardAgg: agg(awardDur),
    bids,
    bidsPerOffer: offers ? bids / offers : 0,
    declined,
    declineRate: bids ? declined / bids : 0,
    wins: [...wins.entries()].map(([id, count]) => ({ id, count })).sort((a, b) => b.count - a.count),
  };
}

/** ms → compact percent for rates. */
export function fmtPct(x: number): string {
  return `${Math.round(x * 100)}%`;
}
