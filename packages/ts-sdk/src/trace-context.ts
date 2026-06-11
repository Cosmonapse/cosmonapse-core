/**
 * @cosmonapse/sdk  -  ambient trace context
 *
 * The (traceId, parentId) of the TASK currently being handled, carried in an
 * AsyncLocalStorage so code that runs *inside* a task but without explicit
 * trace plumbing  -  e.g. a `detects*` hook calling `dendrite.imprint`  -
 * inherits the task's trace instead of minting a fresh one. The TS
 * counterpart to Python's `cosmonapse.envelope.trace_context` ContextVar.
 * Async-safe: each async execution context sees its own binding.
 */

import { AsyncLocalStorage } from "node:async_hooks";

const storage = new AsyncLocalStorage<readonly [string, string]>();

/** Return the ambient (traceId, parentId) of the task being handled, or null. */
export function ambientTrace(): readonly [string, string] | null {
  return storage.getStore() ?? null;
}

/** Bind the ambient (traceId, parentId) for the duration of `fn`. Set by
 *  `Axon.handleTask` around the whole handling pass. */
export function runWithTraceContext<T>(
  traceId: string,
  parentId: string,
  fn: () => T,
): T {
  return storage.run([traceId, parentId] as const, fn);
}
