import { Fragment } from "react";
import type { ReactNode } from "react";
import { C } from "./theme";

// A deliberately small Python tokenizer - enough to make a 30-line module
// readable without pulling a highlighter library into a bundle that
// currently ships React and nothing else. Shared by the read-only source
// panes and by the editable code boxes (which paint it behind a
// transparent textarea).

const KEYWORDS =
  "False|None|True|and|as|assert|async|await|break|class|continue|def|del|elif|else|except|finally|for|from|global|if|import|in|is|lambda|nonlocal|not|or|pass|raise|return|try|while|with|yield";

const TOKEN_RE = new RegExp(
  [
    "(#[^\\n]*)", // 1 comment
    "(\"\"\"[\\s\\S]*?\"\"\"|'''[\\s\\S]*?'''|\"(?:\\\\.|[^\"\\\\\\n])*\"|'(?:\\\\.|[^'\\\\\\n])*')", // 2 string
    "(@[\\w.]+)", // 3 decorator
    `\\b(${KEYWORDS})\\b`, // 4 keyword
    "\\b(\\d+(?:\\.\\d+)?)\\b", // 5 number
    "\\b([A-Z][A-Za-z0-9_]*)\\b", // 6 class / CONSTANT-ish
  ].join("|"),
  "g",
);

const tokenColor = () => [
  C.textFaint, // comment
  C.tkString, // string
  C.effector, // decorator
  C.accent, // keyword
  C.tkNumber, // number
  C.accent2, // capitalised name
];

export function highlight(src: string): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  TOKEN_RE.lastIndex = 0;
  let key = 0;
  while ((m = TOKEN_RE.exec(src)) !== null) {
    if (m.index > last) out.push(<Fragment key={key++}>{src.slice(last, m.index)}</Fragment>);
    const group = m.slice(1).findIndex((g) => g !== undefined);
    out.push(
      <span key={key++} style={{ color: tokenColor()[group] ?? C.text }}>
        {m[0]}
      </span>,
    );
    last = m.index + m[0].length;
  }
  if (last < src.length) out.push(<Fragment key={key++}>{src.slice(last)}</Fragment>);
  return out;
}

/** Font metrics every code surface shares - the overlay only lines up if they match exactly. */
export const CODE_FONT = {
  fontFamily: "ui-monospace,Menlo,monospace",
  fontSize: 14.5,
  lineHeight: "20px",
  tabSize: 4,
  letterSpacing: "normal" as const,
};
