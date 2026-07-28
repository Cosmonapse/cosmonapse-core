import type { BrowseResult, InitError, InitResult, ScaffoldResult } from "./types";

// Thin fetch wrapper over the local API cosmo/commands/_genesis.py exposes.
// Kept as one small module (mirroring prism-ui's useSignalStream.ts being
// the sole point of contact with its server) so the rest of the app never
// touches `fetch` directly.

async function asJson<T>(res: Response): Promise<T> {
  const body = await res.json();
  if (!res.ok) {
    throw body as InitError;
  }
  return body as T;
}

export function browse(path?: string): Promise<BrowseResult> {
  const qs = path ? `?path=${encodeURIComponent(path)}` : "";
  return fetch(`/api/browse${qs}`).then((r) => asJson<BrowseResult>(r));
}

export function initProject(args: {
  name: string;
  path: string;
  namespace: string;
  force?: boolean;
}): Promise<InitResult> {
  return fetch("/api/init", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(args),
  }).then((r) => asJson<InitResult>(r));
}

export function readScaffold(path: string): Promise<ScaffoldResult> {
  return fetch(`/api/scaffold?path=${encodeURIComponent(path)}`).then((r) =>
    asJson<ScaffoldResult>(r),
  );
}
