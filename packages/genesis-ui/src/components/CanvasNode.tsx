import { useRef } from "react";
import { C, MONO } from "../theme";

export type NodeKind = "synapse" | "neuron" | "effector" | "engram";

const KIND_COLOR: Record<NodeKind, string> = {
  synapse: C.synapse,
  neuron: C.neuron,
  effector: C.effector,
  engram: C.engram,
};

const KIND_LABEL: Record<NodeKind, string> = {
  synapse: "Synapse",
  neuron: "Neuron",
  effector: "Effector",
  engram: "Engram",
};

export interface CanvasNodeData {
  key: string;
  kind: NodeKind;
  id: string;
  sublabel?: string;
  x: number;
  y: number;
}

/**
 * A single draggable node on the Genesis canvas. Dragging is plain pointer
 * events (no library) - drag start captures the pointer on the node itself,
 * onDrag reports the node's new x/y in canvas coordinates, drag end is just
 * pointer up. Kept dependency-free to match prism-ui's minimal footprint.
 */
export function CanvasNode({
  node,
  onDrag,
  onSelect,
  selected,
}: {
  node: CanvasNodeData;
  onDrag: (key: string, x: number, y: number) => void;
  onSelect: (key: string) => void;
  selected: boolean;
}) {
  const dragRef = useRef<{ startX: number; startY: number; nodeX: number; nodeY: number } | null>(null);
  const color = KIND_COLOR[node.kind];
  const isSynapse = node.kind === "synapse";
  const size = isSynapse ? 96 : 76;

  function onPointerDown(e: React.PointerEvent) {
    (e.target as Element).setPointerCapture(e.pointerId);
    dragRef.current = { startX: e.clientX, startY: e.clientY, nodeX: node.x, nodeY: node.y };
    onSelect(node.key);
  }

  function onPointerMove(e: React.PointerEvent) {
    if (!dragRef.current) return;
    const dx = e.clientX - dragRef.current.startX;
    const dy = e.clientY - dragRef.current.startY;
    onDrag(node.key, dragRef.current.nodeX + dx, dragRef.current.nodeY + dy);
  }

  function onPointerUp() {
    dragRef.current = null;
  }

  return (
    <div
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      style={{
        position: "absolute",
        left: node.x - size / 2,
        top: node.y - size / 2,
        width: size,
        height: size,
        borderRadius: isSynapse ? 20 : 16,
        background: C.bgCard,
        border: `1.5px solid ${selected ? color : C.borderStrong}`,
        boxShadow: selected ? `0 0 0 3px ${color}33, 0 8px 24px rgba(0,0,0,0.35)` : "0 6px 18px rgba(0,0,0,0.3)",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: 4,
        cursor: "grab",
        userSelect: "none",
        touchAction: "none",
      }}
    >
      <div
        style={{
          width: isSynapse ? 14 : 10,
          height: isSynapse ? 14 : 10,
          borderRadius: "50%",
          background: color,
          boxShadow: `0 0 12px ${color}99`,
        }}
      />
      <div style={{ fontSize: 10, color: C.textFaint, textTransform: "uppercase", letterSpacing: "0.05em" }}>
        {KIND_LABEL[node.kind]}
      </div>
      <div
        style={{
          fontSize: 12,
          fontFamily: MONO,
          color: C.text,
          maxWidth: size - 12,
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
        }}
        title={node.id}
      >
        {node.id}
      </div>
    </div>
  );
}
