import { useCallback, useEffect, useMemo, useState } from "react";
import { ConnectForm } from "./components/ConnectForm";
import type { TabInfo } from "./components/Header";
import { SynapseSession } from "./components/SynapseSession";
import type { SynapseTarget } from "./useSignalStream";

interface SynapseTab {
  id: string;
  target: SynapseTarget;
}

let tabSeq = 0;
const nextTabId = () => `tab_${++tabSeq}`;

function targetFromLocation(): SynapseTarget | null {
  const p = new URLSearchParams(location.search);
  const url = p.get("url");
  if (!url) return null;
  return { url, namespace: p.get("namespace") || "dev" };
}

export function App() {
  const [tabs, setTabs] = useState<SynapseTab[]>(() => {
    const t = targetFromLocation();
    return t ? [{ id: nextTabId(), target: t }] : [];
  });
  const [activeId, setActiveId] = useState<string | null>(tabs[0]?.id ?? null);
  const [adding, setAdding] = useState(false);
  // tab id → live connection state, reported up by each session for the pills.
  const [connState, setConnState] = useState<Record<string, boolean>>({});

  const active = tabs.find((t) => t.id === activeId) ?? null;

  // Keep the URL in sync with the ACTIVE tab so the view stays shareable.
  useEffect(() => {
    if (!active) return;
    const qs = new URLSearchParams({
      url: active.target.url,
      namespace: active.target.namespace,
    }).toString();
    if (location.search !== `?${qs}`) {
      history.replaceState(null, "", `${location.pathname}?${qs}`);
    }
  }, [active]);

  const onConnect = useCallback((t: SynapseTarget) => {
    setTabs((prev) => {
      // Re-attaching to an already-open synapse just focuses its tab.
      const dup = prev.find(
        (x) => x.target.url === t.url && x.target.namespace === t.namespace,
      );
      if (dup) {
        setActiveId(dup.id);
        return prev;
      }
      const tab = { id: nextTabId(), target: t };
      setActiveId(tab.id);
      return [...prev, tab];
    });
    setAdding(false);
  }, []);

  const onCloseTab = useCallback((id: string) => {
    setTabs((prev) => {
      const idx = prev.findIndex((t) => t.id === id);
      const next = prev.filter((t) => t.id !== id);
      setActiveId((cur) => {
        if (cur !== id) return cur;
        return next[Math.max(0, idx - 1)]?.id ?? null;
      });
      if (next.length === 0) history.replaceState(null, "", location.pathname);
      return next;
    });
    setConnState((s) => {
      const { [id]: _, ...rest } = s;
      return rest;
    });
  }, []);

  const onSelectTab = useCallback((id: string) => setActiveId(id), []);
  const onNewTab = useCallback(() => setAdding(true), []);

  // Stable per-tab callback so sessions can report connection state without
  // re-rendering on every parent render.
  const onConnectedChange = useMemo(() => {
    const cache = new Map<string, (c: boolean) => void>();
    return (id: string) => {
      let fn = cache.get(id);
      if (!fn) {
        fn = (c: boolean) =>
          setConnState((s) => (s[id] === c ? s : { ...s, [id]: c }));
        cache.set(id, fn);
      }
      return fn;
    };
  }, []);

  const tabInfos: TabInfo[] = tabs.map((t) => ({
    id: t.id,
    label: t.target.namespace,
    sublabel: shortUrl(t.target.url),
    active: t.id === activeId,
    connected: connState[t.id] ?? false,
  }));

  if (tabs.length === 0 || adding) {
    return (
      <ConnectForm
        initial={tabs.length === 0 ? targetFromLocation() ?? undefined : undefined}
        onConnect={onConnect}
        onCancel={tabs.length > 0 ? () => setAdding(false) : undefined}
      />
    );
  }

  return (
    <>
      {tabs.map((t) => (
        <SynapseSession
          key={t.id}
          target={t.target}
          active={t.id === activeId}
          tabs={tabInfos}
          onConnectedChange={onConnectedChange(t.id)}
          onSelectTab={onSelectTab}
          onCloseTab={onCloseTab}
          onNewTab={onNewTab}
        />
      ))}
    </>
  );
}

function shortUrl(url: string): string {
  return url.replace(/^[a-z]+:\/\//i, "");
}
