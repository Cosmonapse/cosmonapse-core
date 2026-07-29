import type { RecentProject } from "./types";

// Projects Genesis has opened, most recent first. Kept here rather than in a
// component so the start screen and the workspace agree on one list.

const KEY = "genesis:recent-projects";
const SYNAPSE_KEY = "genesis:project-synapse";
const MAX = 8;

export function loadRecents(): RecentProject[] {
  try {
    const raw = localStorage.getItem(KEY);
    const parsed = raw ? (JSON.parse(raw) as RecentProject[]) : [];
    return parsed.filter((p) => p && typeof p.path === "string");
  } catch {
    return [];
  }
}

export function rememberProject(project: RecentProject): RecentProject[] {
  const next = [project, ...loadRecents().filter((p) => p.path !== project.path)].slice(0, MAX);
  try {
    localStorage.setItem(KEY, JSON.stringify(next));
  } catch {
    // best-effort - a full localStorage shouldn't block opening a project
  }
  return next;
}

// Which synapse URL each project was last pointed at. Genesis can't discover
// this - a synapse is a separate process on a port of the user's choosing -
// so the URL you picked is remembered per project and re-probed on open.
// It's a hint, never a claim: the indicator still asks the server.

function loadSynapseMap(): Record<string, string> {
  try {
    const raw = localStorage.getItem(SYNAPSE_KEY);
    const parsed = raw ? (JSON.parse(raw) as Record<string, string>) : {};
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

export function loadSynapseUrl(path: string): string {
  return loadSynapseMap()[path] ?? "";
}

export function rememberSynapseUrl(path: string, url: string): void {
  const map = loadSynapseMap();
  if (url) map[path] = url;
  else delete map[path];
  try {
    localStorage.setItem(SYNAPSE_KEY, JSON.stringify(map));
  } catch {
    // best-effort - a full localStorage shouldn't block starting a synapse
  }
}

export function forgetProject(path: string): RecentProject[] {
  const next = loadRecents().filter((p) => p.path !== path);
  try {
    localStorage.setItem(KEY, JSON.stringify(next));
  } catch {
    // best-effort
  }
  return next;
}
