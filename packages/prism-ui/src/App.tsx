import { useCallback, useEffect, useState } from "react";
import { ConnectForm } from "./components/ConnectForm";
import { SynapseSession } from "./components/SynapseSession";
import {
  initialTabState,
  newTabId,
  saveTabState,
  targetFromLocation,
  type SynapseTab,
  type TabState,
} from "./tabs";
import type { SynapseTarget } from "./useSignalStream";
import { useThemeMode } from "./theme";

export function App() {
  // Subscribing at the root is what makes a theme flip repaint the whole
  // tree, so every literal `C.x` read  -  including SVG fill= attributes,
  // which cannot resolve var()  -  picks up the new palette.
  useThemeMode();
  // Prism holds one session per open synapse. They all stay mounted and
  // streaming; `activeId` only decides which one is on screen.
  const [state, setState] = useState<TabState>(initialTabState);
  const [adding, setAdding] = useState(false);
  const [statuses, setStatuses] = useState<Record<string, boolean>>({});

  const { tabs, activeId } = state;

  useEffect(() => saveTabState(state), [state]);

  // Keep the query string pointed at the active tab so the link stays shareable.
  useEffect(() => {
    const active = tabs.find((t) => t.id === activeId);
    if (!active) {
      if (location.search) history.replaceState(null, "", location.pathname);
      return;
    }
    const qs = new URLSearchParams({
      url: active.url,
      namespace: active.namespace,
    }).toString();
    if (location.search !== `?${qs}`) {
      history.replaceState(null, "", `${location.pathname}?${qs}`);
    }
  }, [tabs, activeId]);

  const openTab = useCallback((t: SynapseTarget) => {
    setState((s) => {
      // Re-attaching a synapse Prism already watches just focuses that tab.
      const existing = s.tabs.find((x) => x.url === t.url && x.namespace === t.namespace);
      if (existing) return { ...s, activeId: existing.id };
      const tab: SynapseTab = { id: newTabId(), url: t.url, namespace: t.namespace };
      return { tabs: [...s.tabs, tab], activeId: tab.id };
    });
    setAdding(false);
  }, []);

  const selectTab = useCallback((id: string) => {
    setState((s) => (s.activeId === id ? s : { ...s, activeId: id }));
  }, []);

  const closeTab = useCallback((id: string) => {
    setState((s) => {
      const i = s.tabs.findIndex((t) => t.id === id);
      if (i < 0) return s;
      const tabs = s.tabs.filter((t) => t.id !== id);
      const activeId =
        s.activeId !== id ? s.activeId : tabs[Math.min(i, tabs.length - 1)]?.id ?? null;
      return { tabs, activeId };
    });
    setStatuses((m) => {
      if (!(id in m)) return m;
      const next = { ...m };
      delete next[id];
      return next;
    });
  }, []);

  const setStatus = useCallback((id: string, connected: boolean) => {
    setStatuses((m) => (m[id] === connected ? m : { ...m, [id]: connected }));
  }, []);

  const showForm = adding || tabs.length === 0;

  return (
    <>
      {tabs.map((tab) => (
        <SynapseSession
          key={tab.id}
          tab={tab}
          active={tab.id === activeId}
          tabs={tabs}
          activeId={activeId}
          statuses={statuses}
          onSelectTab={selectTab}
          onAddTab={() => setAdding(true)}
          onCloseTab={closeTab}
          onStatus={setStatus}
        />
      ))}

      {showForm && (
        <div
          style={{
            position: "fixed",
            inset: 0,
            zIndex: 60,
            background: "var(--bg-overlay)",
            backdropFilter: "blur(8px)",
            WebkitBackdropFilter: "blur(8px)",
          }}
        >
          <ConnectForm
            initial={tabs.length === 0 ? targetFromLocation() ?? undefined : undefined}
            onConnect={openTab}
            onCancel={tabs.length > 0 ? () => setAdding(false) : undefined}
          />
        </div>
      )}
    </>
  );
}
