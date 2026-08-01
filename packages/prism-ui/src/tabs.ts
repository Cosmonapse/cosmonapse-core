/**
 * Prism can watch several synapses at once. Each one is a "tab" - a
 * url + namespace pair with a stable id - and the whole set survives a
 * refresh via localStorage. The query string always mirrors the active tab
 * so a Prism link still points at exactly one synapse.
 */

export interface SynapseTab {
  id: string;
  url: string;
  namespace: string;
}

export interface TabState {
  tabs: SynapseTab[];
  activeId: string | null;
}

const KEY = "cosmonapse.prism.tabs.v1";

export function newTabId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `t_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
}

export function targetFromLocation(): { url: string; namespace: string } | null {
  const p = new URLSearchParams(location.search);
  const url = p.get("url");
  if (!url) return null;
  return { url, namespace: p.get("namespace") || "dev" };
}

function isTab(v: unknown): v is SynapseTab {
  const t = v as SynapseTab | null;
  return !!t && typeof t.id === "string" && typeof t.url === "string" && typeof t.namespace === "string";
}

function load(): TabState {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return { tabs: [], activeId: null };
    const parsed = JSON.parse(raw) as { tabs?: unknown; activeId?: unknown };
    const tabs = Array.isArray(parsed.tabs) ? parsed.tabs.filter(isTab) : [];
    const activeId =
      typeof parsed.activeId === "string" && tabs.some((t) => t.id === parsed.activeId)
        ? parsed.activeId
        : tabs[0]?.id ?? null;
    return { tabs, activeId };
  } catch {
    return { tabs: [], activeId: null };
  }
}

/** Restored tabs, with any ?url=&namespace= link folded in and focused. */
export function initialTabState(): TabState {
  const restored = load();
  const q = targetFromLocation();
  if (!q) return restored;

  const existing = restored.tabs.find((t) => t.url === q.url && t.namespace === q.namespace);
  if (existing) return { ...restored, activeId: existing.id };

  const tab: SynapseTab = { id: newTabId(), url: q.url, namespace: q.namespace };
  return { tabs: [...restored.tabs, tab], activeId: tab.id };
}

export function saveTabState(state: TabState): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(state));
  } catch {
    /* private mode / quota - tabs simply won't persist */
  }
}

export function shortUrl(url: string): string {
  return url.replace(/^[a-z]+:\/\//i, "");
}
