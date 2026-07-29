import { useEffect, useRef, useState } from "react";
import type { CSSProperties } from "react";
import { receptorHttp } from "../api";
import { C, MONO } from "../theme";
import type { InitError, ProxyResult, ReceptorInfo } from "../types";
import { kindColor } from "./CanvasNode";
import { PanelHeader, ghost } from "./TerminalPanel";

/**
 * Minimal Web Speech typings.
 *
 * `SpeechRecognition` is still not in TypeScript's lib.dom (it never became a
 * W3C standard - Chromium ships it prefixed, Safari behind webkit), so the
 * bits actually used are declared here rather than pulling a dependency in
 * for four properties.
 */
interface SpeechRecognitionAlt {
  transcript: string;
}
interface SpeechRecognitionResultLike {
  isFinal: boolean;
  0: SpeechRecognitionAlt;
  length: number;
}
interface SpeechRecognitionEventLike {
  resultIndex: number;
  results: { length: number; [i: number]: SpeechRecognitionResultLike };
}
interface SpeechRecognitionLike {
  lang: string;
  interimResults: boolean;
  continuous: boolean;
  start(): void;
  stop(): void;
  onstart: (() => void) | null;
  onend: (() => void) | null;
  onerror: (() => void) | null;
  onresult: ((e: SpeechRecognitionEventLike) => void) | null;
}
type RecognitionCtor = new () => SpeechRecognitionLike;

const RECOGNITION: RecognitionCtor | undefined =
  (window as unknown as { SpeechRecognition?: RecognitionCtor }).SpeechRecognition ??
  (window as unknown as { webkitSpeechRecognition?: RecognitionCtor }).webkitSpeechRecognition;

const CAN_SPEAK = typeof window !== "undefined" && "speechSynthesis" in window;

interface Turn {
  role: "you" | "bot" | "error";
  text: string;
  ms?: number;
}

/**
 * The ChatReceptor panel.
 *
 * A ChatReceptor already serves its own complete page, and this is
 * deliberately not that page: it is a Genesis-native panel against the same
 * endpoint, so it matches the rest of the app and can annotate each turn with
 * what a tester wants (round-trip time, the session it belongs to). The
 * trade-off is voice - that lives in the served page's own Web Speech code,
 * so open the receptor's URL directly to exercise it.
 *
 * Requests go through the Genesis proxy for the same CORS reason as ApiPanel.
 */
export function ChatPanel({
  projectPath,
  receptor,
  running,
}: {
  projectPath: string;
  receptor: ReceptorInfo;
  running: boolean;
}) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [session, setSession] = useState("default");
  // Default to what the Receptor declares, so the panel opens the way the
  // served page would - but leave it switchable, because a tester wants to
  // exercise the mic without first editing voice= and restarting the brain.
  const [speakOn, setSpeakOn] = useState(Boolean(receptor.config.voice));
  const [listening, setListening] = useState(false);
  const scroller = useRef<HTMLDivElement>(null);
  const recog = useRef<SpeechRecognitionLike | null>(null);
  // send() is re-created every render; the recogniser is built once, so it
  // reaches the current one through a ref instead of closing over a stale copy.
  const sendRef = useRef<(text?: string) => void>(() => {});

  const chatPath = String(receptor.config.path ?? "/chat");
  const greeting = String(receptor.config.greeting ?? "Ask me something.");
  const title = String(receptor.config.title ?? "Chat");
  const historyTurns = Number(receptor.config.history_turns ?? 8);

  useEffect(() => {
    const el = scroller.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [turns, busy]);

  sendRef.current = send;

  /**
   * Read a reply aloud. Same shape as the served page: an utterance in the
   * browser's language, and nothing on the wire - voice is a client-side
   * add-on, so the Receptor still only ever sees text.
   */
  function say(text: string) {
    if (!speakOn || !CAN_SPEAK || !text) return;
    const u = new SpeechSynthesisUtterance(text);
    u.lang = navigator.language || "en-US";
    window.speechSynthesis.speak(u);
  }

  // Build the recogniser once. Interim results stream into the input so you
  // can see it hearing you; the final result submits, which is what makes it
  // feel like talking rather than dictating.
  useEffect(() => {
    if (!RECOGNITION) return;
    const r = new RECOGNITION();
    r.lang = navigator.language || "en-US";
    r.interimResults = true;
    r.continuous = false;
    let base = "";
    r.onstart = () => {
      setListening(true);
      setDraft((d) => {
        base = d;
        return d;
      });
    };
    r.onend = () => setListening(false);
    r.onerror = () => setListening(false);
    r.onresult = (e) => {
      let text = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        text += e.results[i][0].transcript;
      }
      const line = (base ? base + " " : "") + text;
      setDraft(line);
      if (e.results[e.results.length - 1].isFinal) {
        r.stop();
        if (line.trim()) sendRef.current(line);
      }
    };
    recog.current = r;
    return () => {
      r.onresult = null;
      r.onend = null;
      try {
        r.stop();
      } catch {
        /* already stopped */
      }
      recog.current = null;
    };
  }, []);

  // Leaving the panel shouldn't leave the browser talking.
  useEffect(() => () => {
    if (CAN_SPEAK) window.speechSynthesis.cancel();
  }, []);

  function toggleMic() {
    const r = recog.current;
    if (!r) return;
    if (listening) r.stop();
    else r.start();
  }

  function toggleSpeak() {
    setSpeakOn((on) => {
      if (on && CAN_SPEAK) window.speechSynthesis.cancel();
      return !on;
    });
  }

  async function send(override?: string) {
    const message = (override ?? draft).trim();
    if (!message || busy) return;
    setDraft("");
    setTurns((t) => [...t, { role: "you", text: message }]);
    setBusy(true);
    const started = performance.now();
    try {
      const r: ProxyResult = await receptorHttp({
        path: projectPath,
        file: receptor.path,
        method: "POST",
        endpoint: chatPath,
        body: { message, session },
      });
      const ms = Math.round(performance.now() - started);
      if (!r.ok) {
        setTurns((t) => [...t, { role: "error", text: r.error ?? "No reply.", ms }]);
      } else if ((r.status ?? 500) >= 400) {
        // The endpoint maps a receptor timeout to 504 and a terminal ERROR to
        // 500, so the status carries real meaning worth showing verbatim.
        setTurns((t) => [
          ...t,
          { role: "error", text: `${r.status} · ${detail(r)}`, ms: r.elapsed_ms },
        ]);
      } else {
        const reply = (r.json as { reply?: unknown })?.reply;
        const text = render(reply ?? r.text ?? "");
        setTurns((t) => [...t, { role: "bot", text, ms: r.elapsed_ms }]);
        say(text);
      }
    } catch (e) {
      setTurns((t) => [
        ...t,
        { role: "error", text: (e as InitError).error || "Couldn't send that." },
      ]);
    } finally {
      setBusy(false);
    }
  }

  async function reset() {
    setTurns([]);
    // Clear the server's memory too, or the next turn arrives carrying
    // history the panel no longer shows.
    await receptorHttp({
      path: projectPath,
      file: receptor.path,
      method: "POST",
      endpoint: chatPath + "/reset",
      body: { session },
    }).catch(() => undefined);
  }

  const color = kindColor().receptor;

  return (
    <div style={{ flex: 1, display: "flex", flexDirection: "column", minHeight: 0 }}>
      <PanelHeader
        receptor={receptor}
        live={running}
        running={running}
        right={
          <>
            <span style={{ fontSize: 13, fontFamily: MONO, color: C.textFaint, fontWeight: 600, alignSelf: "center" }}>
              session
            </span>
            <input
              value={session}
              onChange={(e) => setSession(e.target.value || "default")}
              title="Turns are kept per session. Change this to start a separate conversation."
              style={{
                width: 92,
                background: "var(--bg-elev)",
                border: `1px solid ${C.border}`,
                borderRadius: 7,
                color: C.text,
                padding: "4px 8px",
                fontFamily: MONO,
                fontSize: 13,
                outline: "none",
              }}
            />
            {CAN_SPEAK && (
              <button
                onClick={toggleSpeak}
                title="Read replies aloud with the browser's speech synthesis."
                style={{
                  ...ghost,
                  color: speakOn ? kindColor().receptor : C.textDim,
                  borderColor: speakOn ? kindColor().receptor + "55" : C.border,
                  background: speakOn ? kindColor().receptor + "12" : "transparent",
                }}
              >
                speak: {speakOn ? "on" : "off"}
              </button>
            )}
            <button onClick={reset} style={ghost}>
              reset
            </button>
          </>
        }
      />

      <div ref={scroller} style={{ flex: 1, overflowY: "auto", padding: "18px 0" }}>
        <div style={{ maxWidth: 720, margin: "0 auto", padding: "0 20px" }}>
          <div style={{ textAlign: "center", marginBottom: 22 }}>
            <div style={{ fontFamily: MONO, fontSize: 14.5, color: C.text }}>{title}</div>
            <div style={{ fontSize: 14, color: C.textFaint, fontWeight: 600, marginTop: 5 }}>{greeting}</div>
            <div style={{ fontSize: 13, color: C.textFaint, fontWeight: 600, marginTop: 7 }}>
              {historyTurns > 0
                ? `carries ${historyTurns} prior turn${historyTurns === 1 ? "" : "s"} per session`
                : "stateless — every turn is independent"}
            </div>
            <div style={{ fontSize: 11, color: C.textFaint, fontWeight: 600, marginTop: 4 }}>
              {voiceNote(Boolean(receptor.config.voice))}
            </div>
          </div>

          {turns.map((t, i) => (
            <Bubble key={i} turn={t} color={color} />
          ))}

          {busy && (
            <div style={{ fontSize: 13.5, fontFamily: MONO, color: C.textFaint, fontWeight: 600, margin: "4px 0 0 62px" }}>
              …
            </div>
          )}
        </div>
      </div>

      <div style={{ flexShrink: 0, borderTop: `1px solid ${C.border}`, padding: "12px 20px" }}>
        <div style={{ maxWidth: 720, margin: "0 auto", display: "flex", gap: 9 }}>
          {RECOGNITION && (
            <button
              onClick={toggleMic}
              disabled={!running}
              title={
                listening
                  ? "Listening — click to stop"
                  : "Speak your message. Recognition runs in the browser; only the text is sent."
              }
              style={micStyle(listening, running)}
            >
              <MicGlyph active={listening} />
            </button>
          )}
          <input
            value={draft}
            disabled={!running}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && send()}
            placeholder={running ? "Say something…" : "brain.py isn't running — press Run above"}
            style={{
              flex: 1,
              background: "var(--bg-elev)",
              border: `1px solid ${C.border}`,
              borderRadius: 9,
              color: C.text,
              padding: "9px 12px",
              fontSize: 14.5,
              outline: "none",
            }}
          />
          <button
            onClick={() => send()}
            disabled={!running || busy || !draft.trim()}
            style={sendStyle(running && !busy && !!draft.trim())}
          >
            Send
          </button>
        </div>
      </div>
    </div>
  );
}

function Bubble({ turn, color }: { turn: Turn; color: string }) {
  const isYou = turn.role === "you";
  const isErr = turn.role === "error";
  return (
    <div style={{ display: "flex", gap: 12, marginBottom: 14, alignItems: "flex-start" }}>
      <span
        style={{
          width: 46,
          flexShrink: 0,
          textAlign: "right",
          fontFamily: MONO,
          fontSize: 13,
          color: isErr ? C.danger : isYou ? C.textFaint : color,
          paddingTop: 3,
        }}
      >
        {isErr ? "error" : turn.role}
      </span>
      <div style={{ minWidth: 0, flex: 1 }}>
        <div
          style={{
            fontSize: 14.5,
            lineHeight: 1.65,
            color: isErr ? C.danger : C.text,
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
            fontFamily: isYou ? "inherit" : MONO,
          }}
        >
          {turn.text}
        </div>
        {turn.ms != null && (
          <div style={{ fontSize: 12.5, fontFamily: MONO, color: C.textFaint, fontWeight: 600, marginTop: 3 }}>
            {turn.ms} ms
          </div>
        )}
      </div>
    </div>
  );
}

/** A reply may be a string or any JSON-able payload the Neuron returned. */
function render(value: unknown): string {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

/** FastAPI puts the message under "detail"; fall back to the raw body. */
function detail(r: ProxyResult): string {
  const d = (r.json as { detail?: unknown })?.detail;
  return typeof d === "string" ? d : render(d ?? r.text ?? "");
}

function sendStyle(enabled: boolean): CSSProperties {
  return {
    padding: "9px 20px",
    borderRadius: 9,
    border: "none",
    background: enabled ? `linear-gradient(135deg, ${C.accent}, ${C.accent2})` : C.bgElev,
    color: enabled ? C.onPrimary : C.textFaint,
    fontWeight: 600,
    fontSize: 14.5,
    cursor: enabled ? "pointer" : "default",
  };
}

/**
 * Whether this Receptor's *own* page would offer voice.
 *
 * The panel offers it either way, because testing the mic shouldn't require
 * editing `voice=` and restarting the brain - but saying so matters, or you
 * could ship a Receptor whose users get no mic while yours worked all along.
 */
function voiceNote(declared: boolean): string {
  return declared
    ? "voice=True — its served page offers the mic and read-back too"
    : "voice=False — the controls here are for testing; its served page won't show them";
}

function MicGlyph({ active }: { active: boolean }) {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <rect x="9" y="2" width="6" height="11" rx="3" />
      <path d="M5 11a7 7 0 0 0 14 0" />
      <line x1="12" y1="18" x2="12" y2="22" />
      {active && (
        <circle cx="12" cy="7.5" r="10" stroke="none" fill="currentColor" opacity="0.16">
          <animate attributeName="opacity" values="0.06;0.24;0.06" dur="1.2s" repeatCount="indefinite" />
        </circle>
      )}
    </svg>
  );
}

function micStyle(listening: boolean, enabled: boolean): CSSProperties {
  const on = listening && enabled;
  return {
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    width: 38,
    flexShrink: 0,
    borderRadius: 9,
    border: `1px solid ${on ? C.danger + "88" : "var(--border)"}`,
    background: on ? C.danger + "18" : "var(--bg-elev)",
    color: !enabled ? C.textFaint : on ? C.danger : C.textDim,
    cursor: enabled ? "pointer" : "default",
  };
}
