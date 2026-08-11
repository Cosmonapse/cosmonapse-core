import { useCallback, useEffect, useRef, useState } from "react";
import { participantKind, receptorRef } from "./types";
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
    // The registry itself is deliberately NOT wiped. It is built from REGISTER
    // signals, which arrive once when a participant joins and never again, so
    // dropping the Map would strip every node of its kind, capabilities and
    // version with no way to recover them short of a reconnect - the Brain
    // View would degrade permanently on a button labelled "clear". Zeroing the
    // counters is what Clear actually means here: it resets what the signal
    // log was counting, and leaves identity alone.
    setNeurons((prev) => {
      const next = new Map<string, NeuronView>();
      for (const [id, n] of prev) {
        next.set(id, { ...n, count: 0, lastType: undefined, lastTs: undefined });
      }
      return next;
    });
  }, []);

  const ingest = useCallback(
    (sig: Signal) => {
      onSignalRef.current?.(sig);
      setTotal((t) => t + 1);
      setSignals((prev) => [sig, ...prev].slice(0, bufferSize));

      // A Receptor is the author of a signal, not its addressee, so it has
      // to be recorded before the directed.id guard below - the TASK a
      // Receptor dispatches is directed at the *target neuron*, and would
      // otherwise credit the whole interaction to that neuron alone.
      const rxid = receptorRef(sig);
      if (rxid) {
        setNeurons((prev) => {
          const next = new Map(prev);
          const ex: NeuronView = next.get(rxid) ?? {
            id: rxid,
            count: 0,
            kind: "receptor",
            capabilities: [],
            firstSeen: sig.ts,
          };
          next.set(rxid, {
            ...ex,
            // Kind is pinned: a Receptor never registers, so there is no
            // later evidence that could reclassify it.
            kind: "receptor",
            count: ex.count + 1,
            lastType: sig.type as SignalType,
            lastTs: sig.ts,
          });
          return next;
        });
      }

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
      // Derived, not hardcoded: Prism is served over http on loopback today,
      // but a `ws://` socket from an https page is blocked outright.
      const scheme = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${scheme}://${location.host}/ws?${qs}`);
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
