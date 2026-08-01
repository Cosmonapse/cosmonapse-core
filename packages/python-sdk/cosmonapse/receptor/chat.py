"""
cosmonapse.receptor.chat
~~~~~~~~~~~~~~~~~~~~~~~~
The conversational Receptor: one chat turn, one dispatch.

This is the simplest of the three shapes and deliberately so - a turn of
text goes in, one TASK goes out, one reply comes back. What ChatReceptor
adds over the raw trio is the part a chat needs and a CLI does not:
per-session history, and a served page to type into.

    rx = ChatReceptor(dendrite=orch, neuron="assistant", voice=True)
    app = rx.app(dendrites=dendrites)      # uvicorn chat:app

    GET  /            the chat page (voice mic + read-back when voice=True)
    POST /chat        {"message": "...", "session": "...", "mode": "wait"}
    GET  /chat/{trace_id}   SSE onto a running turn

History is passed to the Neuron as ``history`` in the TASK input - a list
of ``{"role", "content"}`` - and the session id rides as ``context_ref``
so a memory-backed Neuron can key on it. ``history_turns=0`` makes every
turn stateless.

Voice
-----
Voice is a *client-side add-on*: the served page uses the browser's Web
Speech API - ``SpeechRecognition`` for the mic, ``speechSynthesis`` for
read-back. No audio dependency, no audio bytes over the wire, nothing
extra in the protocol; the Receptor still just sees text. Chromium-based
browsers support both; Safari supports synthesis and, behind a prefix,
recognition. Where recognition is missing the mic button hides itself and
typing still works.
"""

from __future__ import annotations

import json
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from typing import Any

from cosmonapse.envelope import Signal, SignalType
from cosmonapse.receptor.api import ApiReceptor, sse
from cosmonapse.receptor.base import (
    DispatchMode,
    ReceptorError,
    ReceptorTimeout,
    signal_to_jsonable,
)

#: Fields a Neuron might put its prose in, in the order we look.
REPLY_KEYS = ("reply", "response", "answer", "text", "message", "content",
              "output", "report", "result")


def extract_text(value: Any, keys: tuple[str, ...] = REPLY_KEYS) -> str:
    """Pull the human-readable line out of whatever the Neuron returned."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in keys:
            if key in value:
                inner = extract_text(value[key], keys)
                if inner:
                    return inner
        return json.dumps(value, indent=2, default=str)
    if isinstance(value, (list, tuple)):
        return "\n".join(extract_text(v, keys) for v in value)
    return str(value)


class ChatReceptor(ApiReceptor):
    """One chat turn -> one dispatch. Extends the HTTP Receptor.

    ``neuron`` and ``capabilities`` are both optional - a chat fronting a
    pool of Neurons routes by capability instead of naming one.

    ``voice=True`` turns on the browser mic and spoken read-back in the
    served page. ``history_turns`` caps how many prior turns ride along
    with each TASK (0 = stateless).
    """

    def __init__(
        self,
        *,
        dendrite=None,
        neuron: str | None = None,
        capabilities: list[str] | None = None,
        path: str = "/chat",
        title: str = "Cosmonapse Chat",
        voice: bool = False,
        history_turns: int = 8,
        mode: DispatchMode = "wait",
        greeting: str = "Ask me something.",
        **kw: Any,
    ) -> None:
        kw.setdefault("receptor_id", "chat-receptor")
        kw.setdefault("input_key", "message")
        super().__init__(dendrite=dendrite, neuron=neuron,
                         capabilities=capabilities, path=path, mode=mode, **kw)
        self.title = title
        self.voice = voice
        self.greeting = greeting
        self.history_turns = history_turns
        self._history: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=max(history_turns, 0) * 2 or 1)
        )

    # ------------------------------------------------------------------
    # Conversation state
    # ------------------------------------------------------------------

    def history(self, session: str = "default") -> list[dict[str, str]]:
        """The turns kept for a session, oldest first."""
        return list(self._history[session])

    def reset(self, session: str | None = None) -> None:
        """Forget one session, or all of them."""
        if session is None:
            self._history.clear()
        else:
            self._history.pop(session, None)

    def _remember(self, session: str, role: str, content: str) -> None:
        if self.history_turns > 0 and content:
            self._history[session].append({"role": role, "content": content})

    async def build_input(self, raw: Any) -> dict[str, Any]:
        """Wrap the turn, then attach the session's history."""
        session = "default"
        if isinstance(raw, dict) and "session" in raw:
            raw = dict(raw)
            session = str(raw.pop("session") or "default")
        payload = await super().build_input(raw)
        if self.history_turns > 0:
            payload.setdefault("history", self.history(session))
        return payload

    # ------------------------------------------------------------------
    # A turn
    # ------------------------------------------------------------------

    async def turn(
        self, message: str, *, session: str = "default",
        timeout_s: float | None = None, **overrides: Any,
    ) -> str:
        """One message in, one reply string out. The whole chat contract.

        The turn is recorded *after* the dispatch, so ``history`` carries
        the prior conversation only - the current message rides in its own
        field and must not appear twice.
        """
        result = await self.ask(
            {self._input_key: message, "session": session},
            timeout_s=timeout_s, context_ref=session, **overrides,
        )
        reply = extract_text(result)
        self._remember(session, "user", message)
        self._remember(session, "assistant", reply)
        return reply

    async def stream_turn(
        self, message: str, *, session: str = "default",
        timeout_s: float | None = None, **overrides: Any,
    ) -> AsyncIterator[str]:
        """The same turn as SSE - deltas as they arrive, then the reply.

        THOUGHT_DELTA frames stream through as ``event: delta``; the
        terminal Signal arrives as ``event: reply``. A Neuron that does not
        stream simply produces one ``reply`` frame, and the page renders
        the same either way.
        """
        reply = ""
        try:
            async for sig in self.iter_signals(
                {self._input_key: message, "session": session},
                timeout_s=timeout_s, context_ref=session, **overrides,
            ):
                if sig.type is SignalType.THOUGHT_DELTA:
                    yield sse("delta", {"text": _delta_text(sig)})
                elif sig.type is SignalType.ERROR:
                    yield sse("error", {"message": (sig.payload or {}).get(
                        "message", "task failed")})
                elif sig.type in (SignalType.AGENT_OUTPUT, SignalType.FINAL,
                                  SignalType.CLARIFICATION):
                    reply = extract_text(
                        (sig.payload or {}).get("output", sig.payload)
                    )
                    yield sse("reply", {"text": reply,
                                        "trace_id": sig.trace_id})
                else:
                    yield sse("signal", signal_to_jsonable(sig))
        except ReceptorTimeout as exc:
            yield sse("error", {"message": str(exc), "timeout": True})
        except ReceptorError as exc:
            yield sse("error", {"message": str(exc)})
        self._remember(session, "user", message)
        self._remember(session, "assistant", reply)
        yield sse("done", {"ok": True})

    # ------------------------------------------------------------------
    # HTTP surface
    # ------------------------------------------------------------------

    @property
    def router(self):
        """Chat endpoint + the served page, on top of ApiReceptor's routes."""
        from fastapi import APIRouter
        from fastapi.responses import HTMLResponse, StreamingResponse

        router = APIRouter()

        @router.get("/", response_class=HTMLResponse)
        async def page():
            return self.html()

        @router.post(self.path)
        async def chat(body: dict | None = None):
            body = dict(body or {})
            message = str(body.pop("message", "") or "").strip()
            session = str(body.pop("session", "default") or "default")
            mode = str(body.pop("mode", self.default_mode))
            timeout_s = body.pop("timeout_s", self._timeout_s)
            if not message:
                from fastapi import HTTPException
                raise HTTPException(422, "body needs a non-empty 'message'")
            if mode == "stream":
                return StreamingResponse(
                    self.stream_turn(message, session=session,
                                     timeout_s=timeout_s),
                    media_type="text/event-stream",
                    headers={"cache-control": "no-cache",
                             "x-accel-buffering": "no"},
                )
            if mode == "send":
                sig = await self.send({self._input_key: message,
                                       "session": session},
                                      context_ref=session)
                return {"accepted": True, "trace_id": sig.trace_id}
            from fastapi import HTTPException
            try:
                reply = await self.turn(message, session=session,
                                        timeout_s=timeout_s)
            except ReceptorTimeout as exc:
                raise HTTPException(504, str(exc)) from exc
            except ReceptorError as exc:
                raise HTTPException(500, str(exc)) from exc
            return {"reply": reply, "session": session}

        @router.post(self.path + "/reset")
        async def reset(body: dict | None = None):
            self.reset((body or {}).get("session"))
            return {"ok": True}

        @router.get(self.path + "/{trace_id}")
        async def observe(trace_id: str):
            return StreamingResponse(
                self.observe_stream(trace_id),
                media_type="text/event-stream",
                headers={"cache-control": "no-cache"},
            )

        for path, _kind, fn, kw in self._extra_routes:
            router.add_api_route(path, fn, **kw)
        return router

    def app(self, *, title: str | None = None,
            dendrites: list[Any] | None = None, setup=None, teardown=None,
            **kw: Any):
        return super().app(title=title or self.title, dendrites=dendrites,
                           setup=setup, teardown=teardown, **kw)

    # ------------------------------------------------------------------
    # The page
    # ------------------------------------------------------------------

    def html(self) -> str:
        """The single-file chat page. Voice is Web Speech API, client-side."""
        return _PAGE.format(
            title=_escape(self.title),
            greeting=_escape(self.greeting),
            path=self.path,
            voice="true" if self.voice else "false",
        )


def _delta_text(sig: Signal) -> str:
    p = sig.payload or {}
    for key in ("delta", "text", "content", "chunk"):
        if key in p:
            return str(p[key])
    return ""


def _escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{
    --bg: #0b0d10; --panel: #14181d; --line: #232a32; --text: #e6eaef;
    --dim: #8b96a5; --accent: #6ee7b7;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: var(--bg); color: var(--text);
    font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, sans-serif;
    display: flex; flex-direction: column; height: 100vh; }}
  header {{ padding: 14px 18px; border-bottom: 1px solid var(--line);
    display: flex; align-items: center; gap: 10px; }}
  header h1 {{ font-size: 15px; margin: 0; font-weight: 600; }}
  header .sp {{ flex: 1; }}
  header button {{ background: none; border: 1px solid var(--line);
    color: var(--dim); border-radius: 6px; padding: 4px 10px; cursor: pointer;
    font-size: 12px; }}
  header button.on {{ color: var(--accent); border-color: var(--accent); }}
  #log {{ flex: 1; overflow-y: auto; padding: 18px; }}
  .turn {{ max-width: 760px; margin: 0 auto 14px; display: flex; gap: 10px; }}
  .who {{ color: var(--dim); font-size: 11px; text-transform: uppercase;
    letter-spacing: .06em; min-width: 62px; padding-top: 3px; }}
  .body {{ white-space: pre-wrap; word-break: break-word; flex: 1; }}
  .turn.err .body {{ color: #f87171; }}
  form {{ border-top: 1px solid var(--line); padding: 12px 18px;
    display: flex; gap: 8px; max-width: 796px; margin: 0 auto; width: 100%; }}
  input[type=text] {{ flex: 1; background: var(--panel); color: var(--text);
    border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px;
    font: inherit; outline: none; }}
  input[type=text]:focus {{ border-color: var(--accent); }}
  button.send, button.mic {{ background: var(--panel); color: var(--text);
    border: 1px solid var(--line); border-radius: 8px; padding: 0 14px;
    cursor: pointer; font: inherit; }}
  button.mic.rec {{ color: #0b0d10; background: var(--accent);
    border-color: var(--accent); }}
  button[disabled] {{ opacity: .5; cursor: default; }}
</style>
</head>
<body>
<header>
  <h1>{title}</h1>
  <span class="sp"></span>
  <button id="speak" title="Read replies aloud">speak: off</button>
  <button id="reset">reset</button>
</header>
<div id="log"></div>
<form id="form">
  <input type="text" id="msg" placeholder="Type a message" autocomplete="off" autofocus>
  <button type="button" class="mic" id="mic" title="Hold to talk">mic</button>
  <button type="submit" class="send" id="send">send</button>
</form>
<script>
(function () {{
  var VOICE = {voice};
  var PATH = "{path}";
  var SESSION = "s-" + Math.random().toString(36).slice(2, 10);
  var log = document.getElementById("log");
  var form = document.getElementById("form");
  var input = document.getElementById("msg");
  var send = document.getElementById("send");
  var mic = document.getElementById("mic");
  var speakBtn = document.getElementById("speak");
  var speakOn = false;

  function add(who, text, cls) {{
    var t = document.createElement("div");
    t.className = "turn" + (cls ? " " + cls : "");
    var w = document.createElement("div"); w.className = "who"; w.textContent = who;
    var b = document.createElement("div"); b.className = "body"; b.textContent = text;
    t.appendChild(w); t.appendChild(b); log.appendChild(t);
    log.scrollTop = log.scrollHeight;
    return b;
  }}

  if ("{greeting}") add("", "{greeting}");

  // --- voice: Web Speech API, entirely client side -----------------
  var recog = null;
  if (VOICE) {{
    var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (SR) {{
      recog = new SR();
      recog.lang = navigator.language || "en-US";
      recog.interimResults = true;
      recog.continuous = false;
      var base = "";
      recog.onstart = function () {{ mic.classList.add("rec"); base = input.value; }};
      recog.onend = function () {{ mic.classList.remove("rec"); }};
      recog.onerror = function () {{ mic.classList.remove("rec"); }};
      recog.onresult = function (e) {{
        var text = "";
        for (var i = e.resultIndex; i < e.results.length; i++) {{
          text += e.results[i][0].transcript;
        }}
        input.value = (base ? base + " " : "") + text;
        if (e.results[e.results.length - 1].isFinal) {{
          recog.stop();
          if (input.value.trim()) form.requestSubmit();
        }}
      }};
      mic.onclick = function () {{
        if (mic.classList.contains("rec")) {{ recog.stop(); }} else {{ recog.start(); }}
      }};
    }} else {{
      mic.style.display = "none";
    }}
    speakBtn.onclick = function () {{
      speakOn = !speakOn;
      speakBtn.classList.toggle("on", speakOn);
      speakBtn.textContent = "speak: " + (speakOn ? "on" : "off");
      if (!speakOn && window.speechSynthesis) window.speechSynthesis.cancel();
    }};
  }} else {{
    mic.style.display = "none";
    speakBtn.style.display = "none";
  }}

  function say(text) {{
    if (!speakOn || !window.speechSynthesis || !text) return;
    var u = new SpeechSynthesisUtterance(text);
    u.lang = navigator.language || "en-US";
    window.speechSynthesis.speak(u);
  }}

  document.getElementById("reset").onclick = function () {{
    fetch(PATH + "/reset", {{
      method: "POST", headers: {{ "content-type": "application/json" }},
      body: JSON.stringify({{ session: SESSION }})
    }});
    log.innerHTML = "";
  }};

  // --- one turn: POST mode=stream, read SSE ------------------------
  form.onsubmit = async function (e) {{
    e.preventDefault();
    var text = input.value.trim();
    if (!text) return;
    input.value = "";
    add("you", text);
    send.disabled = true;
    var out = add("agent", "");
    var spoken = "";
    try {{
      var res = await fetch(PATH, {{
        method: "POST",
        headers: {{ "content-type": "application/json" }},
        body: JSON.stringify({{ message: text, session: SESSION, mode: "stream" }})
      }});
      if (!res.ok || !res.body) throw new Error("HTTP " + res.status);
      var reader = res.body.getReader();
      var dec = new TextDecoder();
      var buf = "";
      while (true) {{
        var chunk = await reader.read();
        if (chunk.done) break;
        buf += dec.decode(chunk.value, {{ stream: true }});
        var frames = buf.split("\\n\\n");
        buf = frames.pop();
        for (var i = 0; i < frames.length; i++) {{
          var lines = frames[i].split("\\n");
          var ev = "message", data = "";
          for (var j = 0; j < lines.length; j++) {{
            if (lines[j].indexOf("event: ") === 0) ev = lines[j].slice(7);
            else if (lines[j].indexOf("data: ") === 0) data += lines[j].slice(6);
          }}
          if (!data) continue;
          var payload;
          try {{ payload = JSON.parse(data); }} catch (_) {{ continue; }}
          if (ev === "delta") {{ out.textContent += payload.text || ""; }}
          else if (ev === "reply") {{ out.textContent = payload.text || out.textContent;
                                      spoken = out.textContent; }}
          else if (ev === "error") {{ out.parentElement.className = "turn err";
                                      out.textContent = payload.message || "failed"; }}
          log.scrollTop = log.scrollHeight;
        }}
      }}
      if (!out.textContent) out.textContent = "(no reply)";
      say(spoken || out.textContent);
    }} catch (err) {{
      out.parentElement.className = "turn err";
      out.textContent = String(err);
    }} finally {{
      send.disabled = false;
      input.focus();
    }}
  }};
}})();
</script>
</body>
</html>
"""
