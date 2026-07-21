import { useMemo, useState } from "react";
import { C, MONO, colorFor } from "../theme";
import {
  computeMetrics,
  perTaskMetrics,
  computeHealth,
  computeResponsiveness,
  computeHitl,
  computeMemory,
  computeParticipants,
  computeMarket,
  fmtMs,
  fmtPct,
  type TaskMetric,
  type ParticipantMetric,
} from "../metrics";
import { computeConsistency, consistencyColor, type SetupGroup } from "../constellation";
import type { Signal } from "../types";

interface Props {
  signals: Signal[];
}

const TASK_C = colorFor("TASK");
const TOOL_C = colorFor("TOOL_CALL");
const MEM_C = colorFor("RECALL");
const WRITE_C = colorFor("IMPRINT");
const WAIT_C = colorFor("CLARIFICATION");
const PLAN_C = colorFor("PLAN");
const OK_C = colorFor("FINAL");
const ERR_C = colorFor("ERROR");
const ESC_C = colorFor("ESCALATION");
const OFFER_C = colorFor("TASK_OFFER");
const OTHER_C = "#64748b";

export function Metrics({ signals }: Props) {
  const m = useMemo(() => computeMetrics(signals), [signals]);
  const perTask = useMemo(() => perTaskMetrics(signals), [signals]);
  const health = useMemo(() => computeHealth(signals), [signals]);
  const resp = useMemo(() => computeResponsiveness(signals), [signals]);
  const hitl = useMemo(() => computeHitl(signals), [signals]);
  const mem = useMemo(() => computeMemory(signals), [signals]);
  const parts = useMemo(() => computeParticipants(signals), [signals]);
  const market = useMemo(() => computeMarket(signals), [signals]);
  const cons = useMemo(() => computeConsistency(signals), [signals]);
  const permTotal = hitl.approvals + hitl.denials;

  const [active, setActive] = useState<string>("home");
  const [menuOpen, setMenuOpen] = useState(true);

  const sections: { id: string; label: string; el: React.ReactNode }[] = [
    { id: "health", label: "Health", el: (
      <Section color={OK_C} title="Health" help="Task outcomes across the buffer — success, failure, retries and escalations.">
        <Cards>
          <MiniStat color={OK_C} label="Success rate" help="Share of decided tasks (completed vs failed) that reached a FINAL signal. Excludes still-running tasks." value={permOrDash(health.completed + health.failed, fmtPct(health.successRate))} sub={`${health.completed}/${health.completed + health.failed} decided`} />
          <MiniStat color={OK_C} label="Completed" help="Tasks that reached a FINAL signal." value={health.completed} />
          <MiniStat color={ERR_C} label="Failed" help="Tasks that ended in an ERROR without a FINAL." value={health.failed} />
          <MiniStat color={TASK_C} label="In flight" help="Tasks with a TASK signal but no terminal FINAL or ERROR yet." value={health.inFlight} />
          <MiniStat color={C.textDim} label="Retries" help="TASK dispatches whose meta.attempt is greater than 0 — re-attempts after a timeout or failure." value={health.retries} />
          <MiniStat color={ESC_C} label="Escalations" help="Count of ESCALATION signals — work handed up to another handler." value={health.escalations} />
        </Cards>
      </Section>
    ) },
    { id: "latency", label: "Latency", el: (
      <Section color={TASK_C} title="Latency" help="Average timings across whole tasks, tool calls and memory operations.">
        <Cards>
          <MiniStat color={TASK_C} label="Task end-to-end" help="Average wall-clock from a task TASK signal to its last signal, across its whole subtree." value={fmtMs(m.taskAgg.avgMs)} sub={`${m.taskAgg.count} tasks · max ${fmtMs(m.taskAgg.maxMs)}`} />
          <MiniStat color={TASK_C} label="Time to first output" help="Average time from TASK to the first AGENT_OUTPUT — how quickly the agent starts producing." value={fmtMs(resp.firstOutput.avgMs)} sub={`${resp.firstOutput.count} tasks`} />
          <MiniStat color={PLAN_C} label="Time to plan" help="Average time from TASK to the first PLAN signal." value={fmtMs(resp.plan.avgMs)} sub={`${resp.plan.count} tasks`} />
          <MiniStat color={TOOL_C} label="Tool call" help="Average TOOL_CALL to TOOL_RESULT round-trip latency." value={fmtMs(m.toolAgg.avgMs)} sub={`${m.toolAgg.count} calls · max ${fmtMs(m.toolAgg.maxMs)}`} />
          <MiniStat color={MEM_C} label="Memory recall" help="Average RECALL to RECALLED round-trip latency." value={fmtMs(mem.recallAgg.avgMs)} sub={`${mem.recallAgg.count} reads`} />
          <MiniStat color={WRITE_C} label="Memory write" help="Average IMPRINT to IMPRINTED round-trip latency." value={fmtMs(mem.writeAgg.avgMs)} sub={`${mem.writeAgg.count} writes`} />
        </Cards>
      </Section>
    ) },
    { id: "per-task", label: "Per-task breakdown", el: (
      <Section color={TASK_C} title="Per-task breakdown" help="One row per top-level task, rolled up over all of its nested subtasks.">
        <PerTaskTable rows={perTask} />
      </Section>
    ) },
    { id: "composition", label: "Task time composition", el: (
      <Section color={TASK_C} title="Task time composition" help="Where each task spends its wall-clock. Buckets are summed durations and are approximate when operations overlap in time.">
        <Legend
          items={[
            ["tool", TOOL_C],
            ["memory read", MEM_C],
            ["memory write", WRITE_C],
            ["blocked (clarify / permission)", WAIT_C],
            ["compute / other", OTHER_C],
          ]}
        />
        <Composition rows={perTask} />
      </Section>
    ) },
    { id: "hitl", label: "Human-in-the-loop", el: (
      <Section color={WAIT_C} title="Human-in-the-loop" help="Clarification and permission round-trips where the task waits on a person or peer.">
        <Cards>
          <MiniStat color={WAIT_C} label="Clarifications" help="Number of CLARIFICATION to CLARIFICATION_ANSWER round-trips, with their average duration." value={hitl.clarifyAgg.count} sub={`avg ${fmtMs(hitl.clarifyAgg.avgMs)} round-trip`} />
          <MiniStat color={WAIT_C} label="Tasks needing clarify" help="Distinct task traces that emitted at least one CLARIFICATION." value={hitl.clarifyTasks} />
          <MiniStat color={colorFor("PERMISSION")} label="Permission decisions" help="Total PERMISSION_DECISION signals (granted plus denied)." value={permTotal} />
          <MiniStat color={OK_C} label="Approval rate" help="Share of permission decisions with granted = true." value={permTotal ? fmtPct(hitl.approvals / permTotal) : "—"} sub={`${hitl.approvals} allow · ${hitl.denials} deny`} />
          <MiniStat color={colorFor("PERMISSION")} label="Permission round-trip" help="Average PERMISSION to PERMISSION_DECISION latency." value={fmtMs(hitl.permAgg.avgMs)} />
        </Cards>
      </Section>
    ) },
    { id: "memory", label: "Memory effectiveness", el: (
      <Section color={MEM_C} title="Memory effectiveness" help="Engram read/write volume, latency and how often recalls actually return something.">
        <Cards>
          <MiniStat color={MEM_C} label="Recall latency" help="Average RECALL to RECALLED round-trip." value={fmtMs(mem.recallAgg.avgMs)} sub={`${mem.recallCount} recalls`} />
          <MiniStat color={MEM_C} label="Recall hit rate" help="Share of RECALLED responses that returned at least one hit (payload.hits). Shows n/a when responses carry no hits field." value={mem.hitRate == null ? "n/a" : fmtPct(mem.hitRate)} sub={mem.hitsSampled ? `${mem.hitsSampled} sampled` : "no hit data"} />
          <MiniStat color={MEM_C} label="Reads" help="Total RECALL signals issued." value={mem.reads} />
          <MiniStat color={WRITE_C} label="Writes" help="Total IMPRINT signals issued." value={mem.writes} />
          <MiniStat color={WRITE_C} label="Write latency" help="Average IMPRINT to IMPRINTED round-trip." value={fmtMs(mem.writeAgg.avgMs)} />
          <MiniStat color={ERR_C} label="Write errors" help="IMPRINTED signals whose payload carries an error." value={mem.writeErrors} />
        </Cards>
      </Section>
    ) },
    { id: "consistency", label: "Consistency", el: (
      <Section color={C.accent} title="Consistency" help="Whether repeated runs of the same setup produce the same execution graph. A run's graph is who-talked-to-whom (typed edges between neurons, engrams and effectors); a setup groups runs with the same task prompt. Score is the mean pairwise Jaccard similarity of run graphs — 100% means an identical graph every run.">
        <Cards>
          <MiniStat color={consistencyColor(cons.overall)} label="Graph consistency" help="Pair-weighted mean structural similarity across every setup that ran at least twice." value={cons.overall == null ? "—" : fmtPct(cons.overall)} sub={cons.overall == null ? "needs ≥2 runs of a setup" : `${cons.comparedSetups} setup${cons.comparedSetups === 1 ? "" : "s"} compared`} />
          <MiniStat color={TASK_C} label="Setups" help="Distinct task setups seen (grouped by normalized prompt, falling back to target neuron)." value={cons.setups.length} sub={`${cons.totalRuns} runs total`} />
          <MiniStat color={OK_C} label="Repeated setups" help="Setups with at least two runs — the ones consistency can be measured on." value={cons.comparedSetups} />
        </Cards>
        <ConsistencyTable rows={cons.setups} />
      </Section>
    ) },
    { id: "participants", label: "Participants", el: (
      <Section color={C.accent} title="Participants" help="Activity per neuron / engram / effector, keyed by the signal directed.id.">
        <ParticipantTable rows={parts} />
      </Section>
    ) },
    ...(market.offers > 0
      ? [{ id: "market", label: "Market / coordination", el: (
          <Section color={OFFER_C} title="Market / coordination" help="TASK_OFFER to BID to TASK_AWARDED — contention and who wins work.">
            <Cards>
              <MiniStat color={OFFER_C} label="Offers" help="TASK_OFFER signals broadcast; awarded counts the TASK_AWARDED that followed." value={market.offers} sub={`${market.awarded} awarded`} />
              <MiniStat color={OFFER_C} label="Award latency" help="Average TASK_OFFER to TASK_AWARDED time per trace." value={fmtMs(market.awardAgg.avgMs)} />
              <MiniStat color={OFFER_C} label="Bids / offer" help="Average number of BID signals received per offer — contention for work." value={market.bidsPerOffer.toFixed(1)} sub={`${market.bids} bids`} />
              <MiniStat color={ERR_C} label="Decline rate" help="Share of bids that received a TASK_DECLINED." value={fmtPct(market.declineRate)} sub={`${market.declined} declined`} />
            </Cards>
            {market.wins.length > 0 && (
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 12 }}>
                {market.wins.map((w) => (
                  <span key={w.id} style={{ fontFamily: MONO, fontSize: 11, color: C.textDim, background: "rgba(255,255,255,0.04)", border: "1px solid " + C.border, borderRadius: 6, padding: "3px 9px" }}>
                    {w.id} <span style={{ color: OK_C }}>· {w.count} win{w.count === 1 ? "" : "s"}</span>
                  </span>
                ))}
              </div>
            )}
          </Section>
        ) }]
      : []),
    { id: "tools", label: "Longest tool calls", el: (
      <Section color={TOOL_C} title="Longest tool calls" help="The slowest individual TOOL_CALL to TOOL_RESULT round-trips.">
        <BarList color={TOOL_C} rows={m.toolCalls} empty="No completed tool calls yet." />
      </Section>
    ) },
    { id: "recalls", label: "Longest memory recalls", el: (
      <Section color={MEM_C} title="Longest memory recalls" help="The slowest individual RECALL to RECALLED round-trips.">
        <BarList color={MEM_C} rows={m.recalls} empty="No completed memory recalls yet." />
      </Section>
    ) },
  ];

  const validIds = new Set(sections.map((sd) => sd.id));
  const cur = active !== "home" && validIds.has(active) ? active : "home";
  const shown = cur === "home" ? sections : sections.filter((sd) => sd.id === cur);
  const nav = [{ id: "home", label: "Home" }, ...sections.map((sd) => ({ id: sd.id, label: sd.label }))];

  return (
    <div style={{ position: "absolute", top: 64, left: 0, right: 0, bottom: 0, display: "flex", background: "rgba(7,8,12,0.6)" }}>
      {/* collapsible menu */}
      <div
        style={{
          width: menuOpen ? 214 : 0,
          flexShrink: 0,
          overflow: "hidden",
          borderRight: menuOpen ? "1px solid " + C.border : "none",
          background: "rgba(0,0,0,0.2)",
          transition: "width 0.2s ease",
          display: "flex",
          flexDirection: "column",
        }}
      >
        <div style={{ padding: "12px 14px", fontFamily: MONO, fontSize: 10.5, color: C.accent, letterSpacing: "0.14em", textTransform: "uppercase", borderBottom: "1px solid " + C.border, whiteSpace: "nowrap" }}>
          Views
        </div>
        <div style={{ overflowY: "auto", flex: 1 }}>
          {nav.map((item) => {
            const on = item.id === cur;
            return (
              <div
                key={item.id}
                onClick={() => setActive(item.id)}
                style={{
                  padding: "9px 14px",
                  cursor: "pointer",
                  whiteSpace: "nowrap",
                  fontFamily: MONO,
                  fontSize: 11.5,
                  color: on ? C.accent2 : C.textDim,
                  background: on ? "rgba(34,211,238,0.08)" : "transparent",
                  borderLeft: `3px solid ${on ? C.accent2 : "transparent"}`,
                  borderBottom: "1px solid " + C.border,
                }}
              >
                {item.label}
              </div>
            );
          })}
        </div>
      </div>

      {/* content */}
      <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column" }}>
        <div style={{ flexShrink: 0, display: "flex", alignItems: "center", gap: 10, padding: "8px 16px", borderBottom: "1px solid " + C.border }}>
          <button
            onClick={() => setMenuOpen((o) => !o)}
            title={menuOpen ? "Hide menu" : "Show menu"}
            style={{ background: "transparent", border: "1px solid " + C.borderStrong, color: C.textDim, borderRadius: 8, padding: "4px 10px", fontSize: 12, fontFamily: MONO, cursor: "pointer" }}
          >
            {menuOpen ? "‹ menu" : "☰ menu"}
          </button>
          <span style={{ fontFamily: MONO, fontSize: 11.5, color: C.textFaint }}>
            {cur === "home" ? "All metrics" : sections.find((sd) => sd.id === cur)?.label}
          </span>
        </div>
        <div style={{ flex: 1, overflowY: "auto", padding: "24px 32px 56px" }}>
          <div style={{ maxWidth: 1180, margin: "0 auto" }}>
            {signals.length === 0 && (
              <div style={{ textAlign: "center", color: C.textFaint, fontSize: 13, padding: 64 }}>
                Waiting for signals…
              </div>
            )}
            {shown.map((sd) => (
              <div key={sd.id}>{sd.el}</div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

function permOrDash(decided: number, value: string): string {
  return decided ? value : "—";
}

// ── layout primitives ───────────────────────────────────────────────────────
function Cards({ children }: { children: React.ReactNode }) {
  return <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>{children}</div>;
}

function MiniStat({ color, label, value, sub, help }: { color: string; label: string; value: React.ReactNode; sub?: string; help?: string }) {
  return (
    <div
      title={help}
      style={{
        flex: "1 1 150px",
        minWidth: 140,
        background: C.bgCard,
        border: "1px solid " + C.border,
        borderTop: `2px solid ${color}`,
        borderRadius: 10,
        padding: "12px 14px",
      }}
    >
      <div style={{ fontFamily: MONO, fontSize: 9.5, color: C.textFaint, letterSpacing: "0.08em", textTransform: "uppercase", display: "flex", alignItems: "center", gap: 4, cursor: help ? "help" : "default" }}>
        {label}
        {help && <span style={{ opacity: 0.55 }}>ⓘ</span>}
      </div>
      <div style={{ fontFamily: MONO, fontSize: 21, fontWeight: 700, color: C.text, marginTop: 6 }}>{value}</div>
      {sub && <div style={{ fontFamily: MONO, fontSize: 10, color: C.textFaint, marginTop: 3 }}>{sub}</div>}
    </div>
  );
}

function Section({ color, title, help, children }: { color: string; title: string; help?: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 30 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12 }}>
        <span style={{ width: 8, height: 8, borderRadius: 2, background: color, flexShrink: 0 }} />
        <span title={help} style={{ fontFamily: MONO, fontSize: 12.5, color: C.textDim, letterSpacing: "0.04em", display: "inline-flex", alignItems: "center", gap: 5, cursor: help ? "help" : "default" }}>
          {title}
          {help && <span style={{ color: C.textFaint, opacity: 0.55, fontSize: 11 }}>ⓘ</span>}
        </span>
      </div>
      {children}
    </div>
  );
}

function Legend({ items }: { items: [string, string][] }) {
  return (
    <div style={{ display: "flex", gap: 14, flexWrap: "wrap", marginBottom: 10 }}>
      {items.map(([label, color]) => (
        <span key={label} style={{ display: "flex", alignItems: "center", gap: 5, fontFamily: MONO, fontSize: 10, color: C.textFaint }}>
          <span style={{ width: 9, height: 9, borderRadius: 2, background: color }} />
          {label}
        </span>
      ))}
    </div>
  );
}

// ── per-task table ──────────────────────────────────────────────────────────
function PerTaskTable({ rows }: { rows: TaskMetric[] }) {
  if (rows.length === 0) {
    return <div style={{ fontFamily: MONO, fontSize: 12, color: C.textFaint, padding: "6px 2px" }}>No tasks in the buffer yet.</div>;
  }
  const cols = "minmax(170px, 3fr) 84px 88px 116px 116px 64px 68px";
  return (
    <div style={{ border: "1px solid " + C.border, borderRadius: 10, overflow: "hidden" }}>
      <div style={{ display: "grid", gridTemplateColumns: cols, gap: 12, padding: "8px 14px", background: "rgba(255,255,255,0.03)", borderBottom: "1px solid " + C.border, fontFamily: MONO, fontSize: 10, color: C.textFaint, letterSpacing: "0.06em", textTransform: "uppercase" }}>
        <span title="Top-level task (hint or target neuron), with status.">Task</span>
        <span title="End-to-end wall-clock over the task subtree." style={{ textAlign: "right" }}>Total</span>
        <span title="Time to the first AGENT_OUTPUT." style={{ textAlign: "right" }}>1st out</span>
        <span title="Total tool time · number of tool calls in the subtree." style={{ textAlign: "right" }}>Tool calls</span>
        <span title="Total recall time · number of recalls in the subtree." style={{ textAlign: "right" }}>Memory</span>
        <span title="TASK re-attempts (meta.attempt greater than 0)." style={{ textAlign: "right" }}>Retries</span>
        <span title="Number of nested child tasks." style={{ textAlign: "right" }}>Subtasks</span>
      </div>
      {rows.map((r) => {
        const status = r.error ? "error" : r.final ? "final" : "open";
        const statusColor = r.error ? ERR_C : r.final ? OK_C : C.textFaint;
        return (
          <div key={r.trace} style={{ display: "grid", gridTemplateColumns: cols, gap: 12, padding: "9px 14px", borderBottom: "1px solid " + C.border, alignItems: "center" }}>
            <span style={{ minWidth: 0, display: "flex", alignItems: "center", gap: 8 }}>
              <span style={{ width: 7, height: 7, borderRadius: "50%", background: statusColor, flexShrink: 0, boxShadow: `0 0 5px ${statusColor}` }} />
              <span title={r.label} style={{ fontFamily: MONO, fontSize: 11.5, color: C.text, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                {r.label}
              </span>
              <span style={{ fontFamily: MONO, fontSize: 9.5, color: C.textFaint, flexShrink: 0 }}>· {status}</span>
            </span>
            <Num color={TASK_C} bold>{fmtMs(r.durationMs)}</Num>
            <Num>{r.ttfoMs == null ? "—" : fmtMs(r.ttfoMs)}</Num>
            <Num>{r.toolCount === 0 ? "—" : <><span style={{ color: TOOL_C }}>{fmtMs(r.toolMs)}</span> · {r.toolCount}</>}</Num>
            <Num>{r.recallCount === 0 ? "—" : <><span style={{ color: MEM_C }}>{fmtMs(r.recallMs)}</span> · {r.recallCount}</>}</Num>
            <Num color={r.retries ? ERR_C : C.textFaint}>{r.retries || "—"}</Num>
            <Num>{r.subtasks || "—"}</Num>
          </div>
        );
      })}
    </div>
  );
}

function Num({ children, color, bold }: { children: React.ReactNode; color?: string; bold?: boolean }) {
  return (
    <span style={{ textAlign: "right", fontFamily: MONO, fontSize: 11, color: color ?? C.textDim, fontWeight: bold ? 600 : 400 }}>
      {children}
    </span>
  );
}

// ── stacked wall-clock composition ──────────────────────────────────────────
function Composition({ rows }: { rows: TaskMetric[] }) {
  if (rows.length === 0) {
    return <div style={{ fontFamily: MONO, fontSize: 12, color: C.textFaint, padding: "6px 2px" }}>No tasks in the buffer yet.</div>;
  }
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      {rows.slice(0, 30).map((r) => {
        const segs: [number, string, string][] = [
          [r.toolMs, TOOL_C, "tool"],
          [r.recallMs, MEM_C, "memory read"],
          [r.writeMs, WRITE_C, "memory write"],
          [r.waitMs, WAIT_C, "blocked"],
          [r.otherMs, OTHER_C, "compute / other"],
        ];
        const total = segs.reduce((a, [v]) => a + v, 0) || 1;
        return (
          <div key={r.trace} style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <span title={r.label} style={{ width: 200, flexShrink: 0, fontFamily: MONO, fontSize: 11.5, color: C.textDim, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
              {r.label}
            </span>
            <div style={{ flex: 1, height: 16, display: "flex", background: "rgba(255,255,255,0.04)", borderRadius: 4, overflow: "hidden" }}>
              {segs.map(([v, c, name], i) =>
                v > 0 ? <div key={i} title={`${name}: ${fmtMs(v)}`} style={{ width: `${(v / total) * 100}%`, background: c }} /> : null,
              )}
            </div>
            <span style={{ width: 74, flexShrink: 0, textAlign: "right", fontFamily: MONO, fontSize: 11.5, color: C.text }}>{fmtMs(r.durationMs)}</span>
          </div>
        );
      })}
    </div>
  );
}

// ── participants ────────────────────────────────────────────────────────────
function ParticipantTable({ rows }: { rows: ParticipantMetric[] }) {
  if (rows.length === 0) {
    return <div style={{ fontFamily: MONO, fontSize: 12, color: C.textFaint, padding: "6px 2px" }}>No participants seen yet.</div>;
  }
  const cols = "minmax(150px, 3fr) 78px 70px 62px 72px 62px 60px 92px";
  const kindColor = (k: string) => (k === "engram" ? MEM_C : k === "effector" ? TOOL_C : k === "neuron" ? C.accent : C.textFaint);
  return (
    <div style={{ border: "1px solid " + C.border, borderRadius: 10, overflow: "hidden" }}>
      <div style={{ display: "grid", gridTemplateColumns: cols, gap: 10, padding: "8px 14px", background: "rgba(255,255,255,0.03)", borderBottom: "1px solid " + C.border, fontFamily: MONO, fontSize: 10, color: C.textFaint, letterSpacing: "0.06em", textTransform: "uppercase" }}>
        <span title="A neuron / engram / effector id (signal directed.id).">Participant</span>
        <span title="Role from its REGISTER signal — neuron, engram or effector.">Kind</span>
        <span title="Total signals attributed to this participant." style={{ textAlign: "right" }}>Signals</span>
        <span title="TASK signals directed at this participant." style={{ textAlign: "right" }}>Tasks</span>
        <span title="AGENT_OUTPUT and FINAL signals it produced." style={{ textAlign: "right" }}>Outputs</span>
        <span title="ERROR signals attributed to it." style={{ textAlign: "right" }}>Errors</span>
        <span title="Errors as a share of outputs plus errors." style={{ textAlign: "right" }}>Err %</span>
        <span title="Timestamp of its most recent signal." style={{ textAlign: "right" }}>Last seen</span>
      </div>
      {rows.map((p) => (
        <div key={p.id} style={{ display: "grid", gridTemplateColumns: cols, gap: 10, padding: "9px 14px", borderBottom: "1px solid " + C.border, alignItems: "center" }}>
          <span title={p.capabilities.join(", ")} style={{ fontFamily: MONO, fontSize: 11.5, color: C.text, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{p.id}</span>
          <span style={{ fontFamily: MONO, fontSize: 10.5, color: kindColor(p.kind) }}>{p.kind}</span>
          <Num>{p.total}</Num>
          <Num>{p.tasks || "—"}</Num>
          <Num>{p.outputs || "—"}</Num>
          <Num color={p.errors ? ERR_C : C.textFaint}>{p.errors || "—"}</Num>
          <Num color={p.errorRate ? ERR_C : C.textFaint}>{p.outputs + p.errors ? fmtPct(p.errorRate) : "—"}</Num>
          <Num>{safeTime(p.lastSeen)}</Num>
        </div>
      ))}
    </div>
  );
}

// ── longest-N bar list ──────────────────────────────────────────────────────
function BarList({ color, rows, empty }: { color: string; rows: { id: string; label: string; durationMs: number }[]; empty: string }) {
  if (rows.length === 0) {
    return <div style={{ fontFamily: MONO, fontSize: 12, color: C.textFaint, padding: "6px 2px" }}>{empty}</div>;
  }
  const max = Math.max(...rows.map((r) => r.durationMs), 1);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      {rows.slice(0, 20).map((r) => (
        <div key={r.id} style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <span title={r.label} style={{ width: 200, flexShrink: 0, fontFamily: MONO, fontSize: 11.5, color: C.textDim, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
            {r.label}
          </span>
          <div style={{ flex: 1, height: 16, background: "rgba(255,255,255,0.04)", borderRadius: 4, overflow: "hidden" }}>
            <div style={{ width: `${Math.max(2, (r.durationMs / max) * 100)}%`, height: "100%", background: `linear-gradient(90deg, ${color}88, ${color})`, borderRadius: 4 }} />
          </div>
          <span style={{ width: 74, flexShrink: 0, textAlign: "right", fontFamily: MONO, fontSize: 11.5, color: C.text }}>{fmtMs(r.durationMs)}</span>
        </div>
      ))}
    </div>
  );
}

function safeTime(t: string): string {
  const d = new Date(t);
  return isNaN(d.getTime()) ? t : d.toISOString().slice(11, 19);
}

// ── consistency per setup ──────────────────────────────────────────────────────────
function ConsistencyTable({ rows }: { rows: SetupGroup[] }) {
  if (rows.length === 0) {
    return <div style={{ fontFamily: MONO, fontSize: 12, color: C.textFaint, padding: "6px 2px", marginTop: 12 }}>No tasks in the buffer yet.</div>;
  }
  const cols = "minmax(190px, 3fr) 64px 100px minmax(120px, 2fr)";
  return (
    <div style={{ border: "1px solid " + C.border, borderRadius: 10, overflow: "hidden", marginTop: 12 }}>
      <div style={{ display: "grid", gridTemplateColumns: cols, gap: 12, padding: "8px 14px", background: "rgba(255,255,255,0.03)", borderBottom: "1px solid " + C.border, fontFamily: MONO, fontSize: 10, color: C.textFaint, letterSpacing: "0.06em", textTransform: "uppercase" }}>
        <span title="Runs are grouped into a setup by their normalized task prompt.">Setup</span>
        <span title="Number of runs of this setup in the buffer." style={{ textAlign: "right" }}>Runs</span>
        <span title="Mean pairwise Jaccard similarity of the run graphs." style={{ textAlign: "right" }}>Consistency</span>
        <span title="Edges present in every run vs edges seen in any run." style={{ textAlign: "right" }}>Stable edges</span>
      </div>
      {rows.map((g) => {
        const cc = consistencyColor(g.consistency);
        const stable = [...g.edgeFreq.values()].filter((f) => f >= 1).length;
        return (
          <div key={g.key} style={{ display: "grid", gridTemplateColumns: cols, gap: 12, padding: "9px 14px", borderBottom: "1px solid " + C.border, alignItems: "center" }}>
            <span title={g.label} style={{ fontFamily: MONO, fontSize: 11.5, color: C.text, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{g.label}</span>
            <Num>{g.runs.length}</Num>
            <Num color={cc} bold>{g.consistency == null ? "—" : fmtPct(g.consistency)}</Num>
            <Num>{g.runs.length > 1 ? `${stable} / ${g.edgeFreq.size}` : "—"}</Num>
          </div>
        );
      })}
    </div>
  );
}
