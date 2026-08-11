import { useEffect, useRef, useState } from "react";
import type { CSSProperties } from "react";
import { C, MONO } from "../theme";
import type { NeuronPrompt } from "../types";
import { CodeEditor } from "./CodeEditor";

/**
 * The Neuron's system prompt, lifted out of "the rest of the file".
 *
 * An LLM Neuron's prompt is the part of it that gets rewritten most often and
 * is the one thing that never appears in the config form: only ollama, openai
 * and anthropic take a `system=` keyword, so the corpus writes the prompt as a
 * module constant that a `@AXON.before_task` hook folds into the request. That
 * left the most-edited text in the module sitting in a read-only chunk at the
 * bottom of the tab, below every keyword nobody touches.
 *
 * Genesis writes the constant and stops there. Nothing in the SDK looks for a
 * name called SYSTEM - the prompt reaches the model because a hook puts it
 * there - so the card says so when nothing reads it rather than implying a
 * wiring it didn't do.
 */
export function PromptCard({
  prompt,
  accent,
  busy,
  error,
  onSave,
}: {
  prompt: NeuronPrompt | null;
  accent: string;
  busy: boolean;
  error: string | null;
  onSave: (text: string) => void;
}) {
  const saved = prompt?.editable ? prompt.text : "";
  const [draft, setDraft] = useState(saved);

  // Re-seat on the server's copy whenever it changes under us - every edit in
  // this tab round-trips through a fresh model, so the alternative is a box
  // still showing what the file said three saves ago.
  useEffect(() => setDraft(saved), [saved]);

  const dirty = draft !== saved;
  const missing = !prompt;

  return (
    <div style={cardStyle}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}>
        <span style={{ fontSize: 13.5, fontFamily: MONO, color: accent }}>Neuron prompt</span>
        <span style={{ fontSize: 13, color: C.textFaint, fontWeight: 600 }}>
          {missing
            ? "no prompt constant in this module"
            : `${prompt.name} — ${prompt.text.length.toLocaleString()} characters`}
        </span>
        {prompt?.editable === false && (
          <span style={badge(C.warn)} title={prompt.note}>
            read-only
          </span>
        )}
        {(!prompt || prompt.editable) && (
          <div style={{ marginLeft: "auto", display: "flex", gap: 7 }}>
            {dirty && !busy && (
              <button onClick={() => setDraft(saved)} style={ghost}>
                revert
              </button>
            )}
            <button
              onClick={() => dirty && !busy && onSave(draft)}
              disabled={!dirty || busy}
              style={saveStyle(dirty && !busy, accent)}
            >
              {busy ? "saving…" : missing ? "write it" : "save"}
            </button>
          </div>
        )}
      </div>

      {prompt && !prompt.editable ? (
        <>
          <div style={{ fontSize: 13.5, color: C.textDim, lineHeight: 1.55, marginBottom: 10 }}>
            {prompt.name} is {prompt.note}. Saving from here replaces the whole
            constant, which would flatten it — so this one stays yours to edit in
            your editor.
          </div>
          <CodeEditor value={prompt.source} onChange={() => {}} readOnly minRows={1} maxRows={30} />
        </>
      ) : (
        <>
          {missing && (
            <div style={{ fontSize: 13.5, color: C.textDim, lineHeight: 1.55, marginBottom: 10 }}>
              Nothing here holds a system prompt yet. Write one and Genesis adds{" "}
              <code style={{ fontFamily: MONO, color: C.text }}>SYSTEM = (…)</code> at the
              top of the module, under the imports. Feeding it to the model is still
              yours: put it in the request from a{" "}
              <code style={{ fontFamily: MONO, color: C.text }}>@AXON.before_task</code> hook,
              or pass it as <code style={{ fontFamily: MONO, color: C.text }}>system=</code> on
              the declaration if the source takes one.
            </div>
          )}
          <PromptBox value={draft} onChange={setDraft} />
          {prompt && !prompt.used && (
            <div style={{ fontSize: 13, color: C.warn, marginTop: 8, lineHeight: 1.5 }}>
              Nothing in this module reads {prompt.name}. The SDK doesn't look for
              the name — a hook has to put it in the request — so as written this
              prompt never reaches the model.
            </div>
          )}
        </>
      )}
      {error && <div style={errorStyle}>{error}</div>}
    </div>
  );
}

/**
 * A prose box, deliberately not the CodeEditor.
 *
 * A prompt is English, so it wraps and it isn't highlighted: running the Python
 * tokeniser over it would colour every "if" and "return" in the instructions.
 * It still grows with its content, because a prompt is routinely a page long
 * and a fixed six-row window turns editing one into a scrolling exercise.
 */
function PromptBox({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const ref = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const ta = ref.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = `${Math.min(Math.max(ta.scrollHeight, 96), 620)}px`;
  }, [value]);

  return (
    <textarea
      ref={ref}
      value={value}
      spellCheck
      onChange={(e) => onChange(e.target.value)}
      placeholder="You are a research specialist. Using ONLY the supplied web context…"
      style={{
        width: "100%",
        boxSizing: "border-box",
        resize: "vertical",
        display: "block",
        background: C.bgElev,
        border: `1px solid ${C.border}`,
        borderRadius: 8,
        color: C.text,
        padding: 12,
        fontFamily: MONO,
        fontSize: 13.5,
        lineHeight: 1.6,
        whiteSpace: "pre-wrap",
        outline: "none",
      }}
    />
  );
}

const cardStyle: CSSProperties = {
  border: "1px solid var(--border)",
  borderRadius: 11,
  padding: "14px 16px",
  marginTop: 26,
  marginBottom: 14,
  background: "rgba(var(--fg-rgb), 0.012)",
};

const ghost: CSSProperties = {
  background: "transparent",
  border: "1px solid var(--border)",
  borderRadius: 7,
  color: "var(--text-dim)",
  padding: "4px 10px",
  fontSize: 13,
  fontFamily: MONO,
  cursor: "pointer",
};

const errorStyle: CSSProperties = {
  fontSize: 13.5,
  color: "var(--accent3)",
  lineHeight: 1.5,
  background: "rgba(var(--accent3-rgb), 0.07)",
  border: "1px solid rgba(var(--accent3-rgb), 0.25)",
  borderRadius: 7,
  padding: "9px 11px",
  marginTop: 10,
};

function saveStyle(on: boolean, accent: string): CSSProperties {
  return {
    ...ghost,
    color: on ? accent : C.textFaint,
    borderColor: on ? accent + "55" : C.border,
    background: on ? accent + "14" : "transparent",
    cursor: on ? "pointer" : "default",
  };
}

function badge(accent: string): CSSProperties {
  return {
    fontSize: 12.5,
    fontFamily: MONO,
    textTransform: "uppercase",
    letterSpacing: "0.06em",
    color: accent,
    border: `1px solid ${accent}44`,
    background: accent + "12",
    borderRadius: 999,
    padding: "2px 8px",
  };
}
