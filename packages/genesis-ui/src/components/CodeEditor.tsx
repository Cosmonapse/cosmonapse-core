import { useLayoutEffect, useRef } from "react";
import type { CSSProperties } from "react";
import { C } from "../theme";
import { CODE_FONT, highlight } from "../highlight";

const PAD = 12;

/**
 * An editable code surface with syntax highlighting and no dependencies.
 *
 * The standard overlay trick: a transparent-text <textarea> sits on top of a
 * highlighted <pre> that renders the same string. The user types into the
 * real textarea (so selection, undo, IME and native shortcuts all behave),
 * and sees the <pre> through it. The two only stay aligned if every metric
 * that affects layout is identical, which is why the font, padding and
 * line-height come from one shared constant rather than being restated.
 */
export function CodeEditor({
  value,
  onChange,
  minRows = 3,
  maxRows = 24,
  fill = false,
  readOnly = false,
  placeholder,
}: {
  value: string;
  onChange: (next: string) => void;
  /** Grow to fit the content, between these bounds. Ignored when "fill". */
  minRows?: number;
  maxRows?: number;
  /** Fill the parent instead of sizing to content (the helpers editor). */
  fill?: boolean;
  readOnly?: boolean;
  placeholder?: string;
}) {
  const taRef = useRef<HTMLTextAreaElement>(null);
  const preRef = useRef<HTMLPreElement>(null);

  // Keep the painted layer glued to the textarea's scroll position.
  function syncScroll() {
    const ta = taRef.current;
    const pre = preRef.current;
    if (!ta || !pre) return;
    pre.scrollTop = ta.scrollTop;
    pre.scrollLeft = ta.scrollLeft;
  }

  useLayoutEffect(syncScroll, [value]);

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key !== "Tab" || readOnly) return;
    // Tab indents instead of leaving the field - this is a code box, and
    // Python cares. Shift-Tab outdents the current line.
    e.preventDefault();
    const ta = e.currentTarget;
    const { selectionStart: s, selectionEnd: en } = ta;
    if (e.shiftKey) {
      const lineStart = value.lastIndexOf("\n", s - 1) + 1;
      const cut = value.slice(lineStart, lineStart + 4) === "    " ? 4 : 0;
      if (!cut) return;
      onChange(value.slice(0, lineStart) + value.slice(lineStart + cut));
      queueMicrotask(() => ta.setSelectionRange(s - cut, en - cut));
      return;
    }
    onChange(value.slice(0, s) + "    " + value.slice(en));
    queueMicrotask(() => ta.setSelectionRange(s + 4, s + 4));
  }

  const rows = Math.min(Math.max(value.split("\n").length, minRows), maxRows);
  const height = fill ? "100%" : rows * 20 + PAD * 2;

  const shared: CSSProperties = {
    ...CODE_FONT,
    margin: 0,
    padding: PAD,
    border: "none",
    whiteSpace: "pre",
    overflowWrap: "normal",
    boxSizing: "border-box",
    width: "100%",
    height,
  };

  return (
    <div
      style={{
        position: "relative",
        height,
        background: C.bgElev,
        border: `1px solid ${C.border}`,
        borderRadius: 8,
        overflow: "hidden",
      }}
    >
      <pre
        ref={preRef}
        aria-hidden
        style={{ ...shared, position: "absolute", inset: 0, overflow: "hidden", pointerEvents: "none", color: C.text }}
      >
        {value ? highlight(value) : <span style={{ color: C.textFaint, fontWeight: 600, }}>{placeholder ?? ""}</span>}
        {"\n"}
      </pre>
      <textarea
        ref={taRef}
        value={value}
        readOnly={readOnly}
        spellCheck={false}
        wrap="off"
        onChange={(e) => onChange(e.target.value)}
        onScroll={syncScroll}
        onKeyDown={onKeyDown}
        style={{
          ...shared,
          position: "relative",
          background: "transparent",
          color: "transparent",
          caretColor: C.text,
          resize: "none",
          outline: "none",
          overflow: "auto",
          display: "block",
        }}
      />
    </div>
  );
}
