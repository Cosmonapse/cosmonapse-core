import { useCallback, useEffect, useRef, useState } from "react";
import { participantKind } from "./types";
import type { NeuronView, Signal, SignalType } from "./types";

export interface SynapseTarget {
  url: string;
  namespace: string;
}

interface Options {
  paused: boolean;
  /** Fired for every signal as it arrives (before paused filtering is applied
   *  to persistent state). Use for transient animations. */
  onSignal?: (sig: Signal) => void;
  /** Max signals retained in the rolling buffer. */
  bufferSize?: number;
}

interface Stream {
  connected: boolean;
  signals: Signal[];
  neurons: Map<string, NeuronView>;
  total: number;
  clear: () => void;
}

/**
 * Opens the Prism WebSocket bridge and turns its one-JSON-Signal-per-message
 * feed into React state: a rolling signal buffer plus an accumulated neuron
 * registry. Reconnects automatically with a fixed backoff. The bridge is
 * one-way (server → browser); we never send.
 */
export function useSignalStream(
  target: SynapseTarget | null,
  { paused, onSignal, bufferSize = 500 }: Options,
): Stream {
  const [connected, setConnected] = useState(false);
  const [signals, setSignals] = useState<Signal[]>([]);
  const [neurons, setNeurons] = useState<Map<string, NeuronView>>(() => new Map());
  const [total, setTotal] = useState(0);

  const pausedRef = useRef(paused);
  pausedRef.current = paused;
  const onSignalRef = useRef(onSignal);
  onSignalRef.current = onSignal;

  const clear = useCallback(() => {
    setSignals([]);
    setTotal(0);
  }, []);

  const ingest = useCallback(
    (sig: Signal) => {
      onSignalRef.current?.(sig);
      setTotal((t) => t + 1);
      setSignals((prev) => [sig, ...prev].slice(0, bufferSize));

      const nid = sig.directed?.id ?? null;
      if (!nid) return;
      setNeurons((prev) => {
        const next = new Map(prev);
        // One uniform check: kind comes from the REGISTER's `role`. Non-REGISTER
        // signals return null, so a participant keeps the kind it registered
        // with - classification is never inferred from traffic.
        const kind = participantKind(sig);
        const ex: NeuronView = next.get(nid) ?? {
          id: nid,
          count: 0,
          kind: kind ?? "neuron",
          capabilities: [],
          firstSeen: sig.ts,
        };
        const updated: NeuronView = {
          ...ex,
          count: ex.count + 1,
          kind: kind ?? ex.kind,
          lastType: sig.type as SignalType,
          lastTs: sig.ts,
        };
        if (sig.type === "REGISTER") {
          const caps = sig.payload?.capabilities;
          if (Array.isArray(caps)) updated.capabilities = caps as string[];
          const ver = sig.payload?.version;
          if (typeof ver === "string") updated.version = ver;
          updated.deregistered = false;
        }
        if (sig.type === "DEREGISTER") updated.deregistered = true;
        next.set(nid, updated);
        return next;
      });
    },
    [bufferSize],
  );

  useEffect(() => {
    if (!target?.url) return;
    let ws: WebSocket | null = null;
    let closed = false;
    let retry: ReturnType<typeof setTimeout> | undefined;

    const connect = () => {
      const qs = new URLSearchParams({
        url: target.url,
        namespace: target.namespace,
      }).toString();
      ws = new WebSocket(`ws://${location.host}/ws?${qs}`);
      ws.onopen = () => setConnected(true);
      ws.onclose = () => {
        setConnected(false);
        if (!closed) retry = setTimeout(connect, 2000);
      };
      ws.onerror = () => ws?.close();
      ws.onmessage = (e) => {
        if (pausedRef.current) return;
        try {
          ingest(JSON.parse(e.data) as Signal);
        } catch {
          /* ignore malformed frames */
        }
      };
    };

    connect();
    return () => {
      closed = true;
      if (retry) clearTimeout(retry);
      ws?.close();
    };
  }, [target?.url, target?.namespace, ingest]);

  return { connected, signals, neurons, total, clear };
}
