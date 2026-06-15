/**
 * @cosmonapse/sdk  -  retry policy for the dispatch + wait shape.
 *
 * Consumed by `Dendrite.dispatchAndWait({ retry })` and
 * `Dendrite.runWithRetry(...)`. Retry only fits the request/reply shape, where
 * the Dendrite owns the whole arc (dispatch -> wait -> close) and can
 * transparently re-dispatch. The streaming shapes hand the live Pathway to the
 * caller, so retry there would orphan the caller's subscriptions.
 *
 * "Stuck" = no terminal within `timeoutMs` (a TimeoutError from Pathway.wait),
 * the Pathway closing before a terminal (PathwayClosedError), or a returned
 * ERROR flagged `recoverable`. New-trace retries STOP the abandoned attempt
 * before the next try so a stalled worker can't outlive the retry.
 */

import type { Signal } from "./envelope.js";
import { SignalType } from "./envelope.js";
import { PathwayClosedError } from "./pathway.js";

export type RetryOutcome = Signal | Error;

/** Default predicate: retry on timeout, a Pathway closed before a terminal, or
 *  a returned ERROR flagged `recoverable`. FINAL / AGENT_OUTPUT /
 *  CLARIFICATION / PERMISSION are never retried. */
export function defaultRetryOn(outcome: RetryOutcome): boolean {
  if (outcome instanceof PathwayClosedError) return true;
  if (outcome instanceof Error) return outcome.name === "TimeoutError";
  const sig = outcome;
  return (
    sig.type === SignalType.ERROR &&
    Boolean((sig.payload as Record<string, unknown>)?.["recoverable"])
  );
}

export interface RetryStrategy {
  /** Total tries including the first (>= 1). Default 3. */
  maxAttempts?: number;
  /** Per-attempt terminal timeout in ms. Default 30000. */
  timeoutMs?: number;
  /** attempt -> ms to sleep before the next try (0-based). Default 0. */
  backoffMs?: (attempt: number) => number;
  /** outcome -> whether to retry. Default {@link defaultRetryOn}. */
  retryOn?: (outcome: RetryOutcome) => boolean;
  /** Fresh trace per attempt (and STOP the abandoned one). Default true. */
  newTrace?: boolean;
  /** Also roll back the abandoned attempt's Engram writes. Default false. */
  rollbackOnRetry?: boolean;
  /** Hook fired just before a re-dispatch. */
  onRetry?: (attempt: number, outcome: RetryOutcome) => void;
  /** Reason carried on the preemptive STOP. Default "retry". */
  reason?: string;
}
