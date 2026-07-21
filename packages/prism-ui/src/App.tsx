import { useCallback, useEffect, useState } from "react";
import { ConnectForm } from "./components/ConnectForm";
import { SynapseSession } from "./components/SynapseSession";
import type { SynapseTarget } from "./useSignalStream";

function targetFromLocation(): SynapseTarget | null {
  const p = new URLSearchParams(location.search);
  const url = p.get("url");
  if (!url) return null;
  return { url, namespace: p.get("namespace") || "dev" };
}

export function App() {
  // Prism monitors a single synapse/namespace; the three views (Brain / Tree /
  // Metrics) all read from it. Reconnecting to another synapse replaces it.
  const [target, setTarget] = useState<SynapseTarget | null>(() => targetFromLocation());

  // Keep the URL in sync so the view stays shareable.
  useEffect(() => {
    if (!target) {
      if (location.search) history.replaceState(null, "", location.pathname);
      return;
    }
    const qs = new URLSearchParams({
      url: target.url,
      namespace: target.namespace,
    }).toString();
    if (location.search !== `?${qs}`) {
      history.replaceState(null, "", `${location.pathname}?${qs}`);
    }
  }, [target]);

  const onConnect = useCallback((t: SynapseTarget) => setTarget(t), []);
  const onDisconnect = useCallback(() => setTarget(null), []);

  if (!target) {
    return <ConnectForm initial={targetFromLocation() ?? undefined} onConnect={onConnect} />;
  }

  return <SynapseSession target={target} onDisconnect={onDisconnect} />;
}
