"""
Tests for the Receptor interface layer.

A Receptor is the edge - CLI, HTTP, chat - over the dispatch trio. These
exercise it against a real MemorySynapse with a real worker Axon, so the
TASK genuinely goes out and the AGENT_OUTPUT genuinely comes back:
nothing here is a mock of the protocol.

Covered:
  * the trio: send / wait / stream all reach the same Neuron
  * input shaping (input_key, @on_input) and result shaping (@on_result)
  * @on_signal progress hooks fire on the intermediate Signals
  * ERROR -> ReceptorError, deadline -> ReceptorTimeout, no Dendrite ->
    ReceptorUnbound, no target -> an open call
  * CliReceptor builds argparse from the command signature, routes a bare
    goal to the default command, and answers local commands without
    dispatching
  * ApiReceptor parses the body into (input, mode, timeout) and serves
    all three modes over HTTP
  * ChatReceptor keeps per-session history and streams a turn as SSE
  * importing cosmonapse does not import FastAPI
"""

import asyncio
import sys

import pytest

from cosmonapse import (
    Axon,
    CliReceptor,
    Dendrite,
    MemorySynapse,
    Receptor,
    ReceptorError,
    ReceptorTimeout,
    ReceptorUnbound,
    SignalType,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# A minimal real stack: one worker Axon, one orchestrator Dendrite.
# ---------------------------------------------------------------------------


async def echo_neuron(payload: dict, context: dict | None = None) -> dict:
    """Echoes back what it was asked, so assertions can see the TASK input."""
    if payload.get("boom"):
        raise RuntimeError("neuron exploded")
    if payload.get("hang"):
        await asyncio.sleep(5)
    return {
        "reply": f"echo: {payload.get('prompt') or payload.get('message') or ''}",
        "seen": payload,
    }


class _Stack:
    def __init__(self, synapse, worker, orch):
        self.synapse, self.worker, self.orch = synapse, worker, orch

    async def aclose(self):
        await self.worker.stop()
        await self.orch.stop()
        await self.synapse.close()


async def make_stack(namespace: str = "rx-test") -> _Stack:
    synapse = MemorySynapse()
    await synapse.connect()
    worker = Dendrite(synapse=synapse, namespace=namespace,
                      dendrite_id="worker-d", role="worker")
    worker.attach_axon(Axon(neuron_id="echo", neuron_fn=echo_neuron,
                            capabilities=["echo"]))
    orch = Dendrite(synapse=synapse, namespace=namespace,
                    dendrite_id="orch-d", role="orchestrator")
    await worker.start()
    await orch.start()
    return _Stack(synapse, worker, orch)


@pytest.fixture
async def stack():
    st = await make_stack()
    try:
        yield st
    finally:
        await st.aclose()


class _Rx(Receptor):
    """The base class is abstract only by convention - nothing to implement."""


# ---------------------------------------------------------------------------
# The dispatch trio
# ---------------------------------------------------------------------------


async def test_wait_returns_the_rendered_output(stack):
    rx = _Rx(dendrite=stack.orch, neuron="echo")
    result = await rx.ask("hello", timeout_s=5)
    assert result["reply"] == "echo: hello"


async def test_send_is_fire_and_forget(stack):
    rx = _Rx(dendrite=stack.orch, neuron="echo")
    sig = await rx.send("hello")
    assert sig.type is SignalType.TASK
    assert sig.trace_id.startswith("trc_")
    # meta carries the receptor id, so a trace is attributable to its edge
    assert (sig.meta or {}).get("receptor") == "receptor"


async def test_stream_yields_the_terminal_signal(stack):
    rx = _Rx(dendrite=stack.orch, neuron="echo")
    seen = []
    async for sig in rx.iter_signals("hello", timeout_s=5):
        seen.append(sig.type)
        if sig.type in (SignalType.AGENT_OUTPUT, SignalType.FINAL):
            break
    assert SignalType.AGENT_OUTPUT in seen


async def test_receive_dispatches_by_mode(stack):
    rx = _Rx(dendrite=stack.orch, neuron="echo")
    assert (await rx.receive("a", mode="wait", timeout_s=5))["reply"] == "echo: a"
    assert (await rx.receive("a", mode="send")).type is SignalType.TASK
    pw = await rx.receive("a", mode="stream")
    await pw.close()
    with pytest.raises(ValueError):
        await rx.receive("a", mode="teleport")


async def test_capability_routing_works_as_a_target(stack):
    rx = _Rx(dendrite=stack.orch, capabilities=["echo"])
    result = await rx.ask("routed", timeout_s=5)
    assert result["reply"] == "echo: routed"


# ---------------------------------------------------------------------------
# Shaping hooks
# ---------------------------------------------------------------------------


async def test_input_key_wraps_a_bare_string(stack):
    rx = _Rx(dendrite=stack.orch, neuron="echo", input_key="prompt")
    result = await rx.ask("wrapped", timeout_s=5)
    assert result["seen"]["prompt"] == "wrapped"


async def test_dict_input_passes_through_untouched(stack):
    rx = _Rx(dendrite=stack.orch, neuron="echo")
    result = await rx.ask({"prompt": "x", "extra": 7}, timeout_s=5)
    assert result["seen"]["extra"] == 7


async def test_on_input_replaces_the_default_wrapping(stack):
    rx = _Rx(dendrite=stack.orch, neuron="echo")

    @rx.on_input
    def build(raw):
        return {"prompt": raw.upper(), "built": True}

    result = await rx.ask("shout", timeout_s=5)
    assert result["seen"]["built"] is True
    assert result["reply"] == "echo: SHOUT"


async def test_on_input_may_be_async(stack):
    rx = _Rx(dendrite=stack.orch, neuron="echo")

    @rx.on_input
    async def build(raw):
        await asyncio.sleep(0)
        return {"prompt": f"[{raw}]"}

    assert (await rx.ask("a", timeout_s=5))["reply"] == "echo: [a]"


async def test_on_result_shapes_the_reply(stack):
    rx = _Rx(dendrite=stack.orch, neuron="echo")

    @rx.on_result
    def render(sig):
        return sig.payload["output"]["reply"].upper()

    assert await rx.ask("quiet", timeout_s=5) == "ECHO: QUIET"


async def test_on_signal_observes_the_trace(stack):
    rx = _Rx(dendrite=stack.orch, neuron="echo")
    seen = []

    @rx.on_signal(SignalType.AGENT_OUTPUT)
    async def watch(sig):
        seen.append(sig.type)

    await rx.ask("hi", timeout_s=5)
    assert seen == [SignalType.AGENT_OUTPUT]


async def test_a_broken_progress_hook_does_not_break_the_trace(stack):
    rx = _Rx(dendrite=stack.orch, neuron="echo")

    @rx.on_signal(SignalType.AGENT_OUTPUT)
    async def bad(sig):
        raise RuntimeError("hook is broken")

    assert (await rx.ask("still fine", timeout_s=5))["reply"] == "echo: still fine"


# ---------------------------------------------------------------------------
# Failure surfaces
# ---------------------------------------------------------------------------


async def test_error_signal_becomes_receptor_error(stack):
    rx = _Rx(dendrite=stack.orch, neuron="echo")
    with pytest.raises(ReceptorError):
        await rx.ask({"boom": True}, timeout_s=5)


async def test_deadline_becomes_receptor_timeout(stack):
    rx = _Rx(dendrite=stack.orch, neuron="echo")
    with pytest.raises(ReceptorTimeout):
        await rx.ask({"hang": True}, timeout_s=0.2)


async def test_on_failure_can_swallow(stack):
    rx = _Rx(dendrite=stack.orch, neuron="echo")

    @rx.on_failure
    def caught(exc):
        return {"failed": type(exc).__name__}

    # every runtime failure routes through the hook - deadline...
    assert (await rx.ask({"hang": True}, timeout_s=0.2))["failed"] == "ReceptorTimeout"
    # ...and a terminal ERROR Signal, which is the common case
    assert (await rx.ask({"boom": True}, timeout_s=5))["failed"] == "ReceptorError"


async def test_on_failure_never_swallows_a_wiring_mistake(stack):
    # ReceptorUnbound is a bug in the caller's wiring, not a runtime
    # failure - a hook must not be able to hide it. An unbound *Dendrite*
    # is that mistake; an unset target is not (see the open-call tests).
    rx = _Rx(neuron="echo")

    @rx.on_failure
    def caught(exc):
        return "swallowed"

    with pytest.raises(ReceptorUnbound):
        await rx.ask("nowhere")


async def test_no_target_is_an_open_call(stack):
    # Neither neuron= nor capabilities= is legal: the TASK goes out
    # unaddressed rather than raising. The stack's Axon is an ordinary one,
    # so nothing answers and the deadline is what surfaces.
    rx = _Rx(dendrite=stack.orch)
    sig = await rx.send("to whoever wants it")
    assert sig.type is SignalType.TASK
    assert sig.directed is None
    assert not (sig.payload.get("capabilities") or [])

    with pytest.raises(ReceptorTimeout):
        await rx.ask("to whoever wants it", timeout_s=0.3)


async def test_open_call_reaches_a_catch_all_axon(stack):
    # ...and with an Axon that opted in, the same untargeted ask resolves.
    async def anyone(payload: dict, context: dict | None = None) -> dict:
        return {"reply": "caught"}

    await stack.worker.add_axon(Axon(
        neuron_id="sponge", neuron_fn=anyone, catch_all=True, version="0.0.1",
    ))

    rx = _Rx(dendrite=stack.orch)
    assert (await rx.ask("to whoever wants it", timeout_s=5))["reply"] == "caught"


async def test_per_call_target_overrides_the_constructor(stack):
    rx = _Rx(dendrite=stack.orch)
    result = await rx.ask("late binding", neuron="echo", timeout_s=5)
    assert result["reply"] == "echo: late binding"


# ---------------------------------------------------------------------------
# CliReceptor
# ---------------------------------------------------------------------------


async def test_cli_builds_argparse_from_the_signature(stack):
    rx = CliReceptor(dendrite=stack.orch, neuron="echo", prog="t")

    @rx.command()
    def run(prompt: str, times: int = 1, loud: bool = False):
        return {"prompt": prompt, "times": times, "loud": loud}

    ns = rx.parser().parse_args(["run", "hello", "there", "--times", "3", "--loud"])
    kwargs = rx._commands["run"].kwargs_from(ns)
    assert kwargs == {"prompt": "hello there", "times": 3, "loud": True}


async def test_cli_routes_a_bare_goal_to_the_default_command(stack, capsys):
    rx = CliReceptor(dendrite=stack.orch, neuron="echo", prog="t")

    @rx.command()
    def run(prompt: str):
        return {"prompt": prompt}

    code = await rx.run(["research", "the", "collatz", "conjecture"])
    assert code == 0
    assert "echo: research the collatz conjecture" in capsys.readouterr().out


async def test_cli_local_command_never_dispatches(stack, capsys):
    rx = CliReceptor(dendrite=stack.orch, neuron="echo", prog="t")

    @rx.command()
    def run(prompt: str):
        return {"prompt": prompt}

    @rx.command("memory", local=True, help="what it remembers")
    def memory():
        return {"entries": 42}

    assert await rx.run(["memory"]) == 0
    assert "42" in capsys.readouterr().out


async def test_cli_send_mode_prints_the_trace(stack, capsys):
    rx = CliReceptor(dendrite=stack.orch, neuron="echo", prog="t")

    @rx.command()
    def run(prompt: str):
        return {"prompt": prompt}

    assert await rx.run(["--send", "fire"]) == 0
    assert "trc_" in capsys.readouterr().out


async def test_cli_on_print_takes_over_rendering(stack, capsys):
    rx = CliReceptor(dendrite=stack.orch, neuron="echo", prog="t")

    @rx.command()
    def run(prompt: str):
        return {"prompt": prompt}

    @rx.on_print
    def show(result):
        print("REPLY:", result["reply"])

    await rx.run(["hi"])
    assert "REPLY: echo: hi" in capsys.readouterr().out


async def test_cli_repl_line_reaches_an_all_optional_command(stack):
    """Regression: a REPL line was dropped when the default command's only
    parameter had a default (argparse makes it a --flag, not a positional)."""
    from cosmonapse.receptor.cli import _repl_kwargs

    rx = CliReceptor(dendrite=stack.orch, neuron="echo", prog="t")

    @rx.command()
    def greet(prompt: str = "world", times: int = 1):
        return {"prompt": prompt, "times": times}

    assert _repl_kwargs(rx._commands["greet"], "Aqib") == {
        "prompt": "Aqib", "times": 1,
    }
    # an empty line still falls back to the declared default
    assert _repl_kwargs(rx._commands["greet"], "")["prompt"] == "world"


async def test_cli_repl_line_prefers_a_required_positional(stack):
    from cosmonapse.receptor.cli import _repl_kwargs

    rx = CliReceptor(dendrite=stack.orch, neuron="echo", prog="t")

    @rx.command()
    def ask(prompt: str, tone: str = "plain"):
        return {"prompt": prompt, "tone": tone}

    assert _repl_kwargs(rx._commands["ask"], "hello") == {
        "prompt": "hello", "tone": "plain",
    }


async def test_cli_rejects_varargs_commands(stack):
    rx = CliReceptor(dendrite=stack.orch, neuron="echo")
    with pytest.raises(TypeError):
        @rx.command()
        def run(*words):
            return " ".join(words)


# ---------------------------------------------------------------------------
# ApiReceptor
# ---------------------------------------------------------------------------


async def test_api_parses_the_body(stack):
    from cosmonapse.receptor.api import ApiReceptor

    rx = ApiReceptor(dendrite=stack.orch, neuron="echo", path="run",
                     timeout_s=30)
    raw, mode, timeout_s, overrides = rx.parse(
        {"input": "hello", "mode": "stream", "timeout_s": 5}
    )
    assert (raw, mode, timeout_s) == ("hello", "stream", 5.0)
    # a bare payload with no envelope still works
    raw, mode, _, _ = rx.parse({"goal": "x"})
    assert raw == {"goal": "x"} and mode == "wait"
    # timeouts are clamped, unknown modes rejected
    _, _, clamped, _ = rx.parse({"input": "x", "timeout_s": 10_000})
    assert clamped == rx.max_timeout_s
    with pytest.raises(ValueError):
        rx.parse({"input": "x", "mode": "teleport"})


async def test_api_path_is_normalised(stack):
    from cosmonapse.receptor.api import ApiReceptor

    assert ApiReceptor(dendrite=stack.orch, path="run").path == "/run"
    assert ApiReceptor(dendrite=stack.orch, path="/run/").path == "/run"


async def test_api_serves_all_three_modes(stack):
    import httpx

    from cosmonapse.receptor.api import ApiReceptor

    rx = ApiReceptor(dendrite=stack.orch, neuron="echo", path="/run",
                     timeout_s=5)
    app = rx.app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://t") as client:
        r = await client.post("/run", json={"input": "hello"})
        assert r.status_code == 200
        assert r.json()["reply"] == "echo: hello"

        r = await client.post("/run", json={"input": "hi", "mode": "send"})
        assert r.status_code == 200 and r.json()["accepted"] is True

        r = await client.post("/run", json={"input": "hi", "mode": "stream"})
        assert r.status_code == 200
        assert "event: agent_output" in r.text
        assert "event: done" in r.text


async def test_api_maps_failures_to_status_codes(stack):
    import httpx

    from cosmonapse.receptor.api import ApiReceptor

    rx = ApiReceptor(dendrite=stack.orch, neuron="echo", path="/run",
                     timeout_s=0.2)
    transport = httpx.ASGITransport(app=rx.app())
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://t") as client:
        assert (await client.post("/run", json={"input": {"hang": True}})
                ).status_code == 504
        assert (await client.post("/run", json={"input": {"boom": True},
                                                "timeout_s": 5})
                ).status_code == 500
        assert (await client.post("/run", json={"input": "x", "mode": "nope"})
                ).status_code == 422


async def test_api_extra_route(stack):
    import httpx

    from cosmonapse.receptor.api import ApiReceptor

    rx = ApiReceptor(dendrite=stack.orch, neuron="echo", path="/run")

    @rx.route("/memory")
    async def memory():
        return {"entries": 3}

    transport = httpx.ASGITransport(app=rx.app())
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://t") as client:
        assert (await client.get("/memory")).json() == {"entries": 3}


# ---------------------------------------------------------------------------
# ChatReceptor
# ---------------------------------------------------------------------------


async def test_chat_turn_returns_text_and_keeps_history(stack):
    from cosmonapse.receptor.chat import ChatReceptor

    rx = ChatReceptor(dendrite=stack.orch, neuron="echo", timeout_s=5)
    assert await rx.turn("hello", session="s1") == "echo: hello"
    assert [t["role"] for t in rx.history("s1")] == ["user", "assistant"]
    # the second turn carries the first
    await rx.turn("again", session="s1")
    assert len(rx.history("s1")) == 4
    # sessions are independent
    assert rx.history("s2") == []
    rx.reset("s1")
    assert rx.history("s1") == []


async def test_chat_history_rides_along_in_the_task(stack):
    from cosmonapse.receptor.chat import ChatReceptor

    rx = ChatReceptor(dendrite=stack.orch, neuron="echo", timeout_s=5)
    await rx.turn("first", session="s")
    payload = await rx.build_input({"message": "second", "session": "s"})
    # prior turns only - the current message must not appear twice
    assert [t["content"] for t in payload["history"]] == ["first", "echo: first"]


async def test_chat_stateless_when_history_turns_is_zero(stack):
    from cosmonapse.receptor.chat import ChatReceptor

    rx = ChatReceptor(dendrite=stack.orch, neuron="echo", history_turns=0,
                      timeout_s=5)
    await rx.turn("hello")
    assert rx.history() == []
    assert "history" not in await rx.build_input("x")


async def test_chat_extract_text_finds_the_prose():
    from cosmonapse.receptor.chat import extract_text

    assert extract_text("plain") == "plain"
    assert extract_text({"reply": "a"}) == "a"
    assert extract_text({"output": {"answer": "b"}}) == "b"
    assert extract_text(None) == ""
    assert "unknown" in extract_text({"unknown": "c"})


async def test_chat_serves_the_page_and_streams_a_turn(stack):
    import httpx

    from cosmonapse.receptor.chat import ChatReceptor

    rx = ChatReceptor(dendrite=stack.orch, neuron="echo", voice=True,
                      timeout_s=5)
    transport = httpx.ASGITransport(app=rx.app())
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://t") as client:
        page = await client.get("/")
        assert page.status_code == 200
        assert "webkitSpeechRecognition" in page.text
        assert "{" in page.text and "{{" not in page.text  # format() fully applied

        r = await client.post("/chat", json={"message": "hey"})
        assert r.json()["reply"] == "echo: hey"

        r = await client.post("/chat", json={"message": "hey", "mode": "stream"})
        assert "event: reply" in r.text and "event: done" in r.text

        assert (await client.post("/chat", json={"message": "  "})
                ).status_code == 422
        assert (await client.post("/chat/reset", json={})).json() == {"ok": True}


async def test_chat_page_hides_voice_when_off(stack):
    from cosmonapse.receptor.chat import ChatReceptor

    rx = ChatReceptor(dendrite=stack.orch, neuron="echo", voice=False)
    assert "var VOICE = false;" in rx.html()


# ---------------------------------------------------------------------------
# Late binding and the lifespan
# ---------------------------------------------------------------------------


async def test_every_backend_may_be_built_without_a_dendrite():
    # An ASGI app is imported before there is a loop to connect a synapse
    # on, so the Receptor has to be constructible unbound.
    from cosmonapse.receptor.api import ApiReceptor
    from cosmonapse.receptor.chat import ChatReceptor

    for cls in (CliReceptor, ApiReceptor, ChatReceptor):
        rx = cls(neuron="echo")
        assert rx.bound is False
        with pytest.raises(ReceptorUnbound):
            _ = rx.dendrite


async def test_bind_attaches_the_orchestrator(stack):
    rx = _Rx(neuron="echo")
    assert rx.bind(stack.orch) is rx
    assert rx.bound and rx.dendrite is stack.orch
    assert (await rx.ask("late", timeout_s=5))["reply"] == "echo: late"


async def test_lifespan_setup_binds_and_teardown_runs_after_stop():
    """Regression: teardown used to run inside the exit stack, so the
    synapse closed before the Dendrites stopped and DEREGISTER failed."""
    from cosmonapse.receptor.api import ApiReceptor

    rx = ApiReceptor(neuron="echo", path="/run", timeout_s=5)
    order = []

    async def setup():
        st = await make_stack("rx-lifespan")
        order.append("setup")
        return [st.worker, st.orch], st.orch

    async def teardown():
        order.append("teardown")

    assert rx.bound is False
    async with rx.lifespan(setup=setup, teardown=teardown):
        assert rx.bound is True
        assert (await rx.ask("inside", timeout_s=5))["reply"] == "echo: inside"
    assert order == ["setup", "teardown"]


async def test_lifespan_runs_teardown_even_on_failure():
    from cosmonapse.receptor.api import ApiReceptor

    rx = ApiReceptor(neuron="echo")
    ran = []

    async def teardown():
        ran.append(True)

    with pytest.raises(RuntimeError):
        async with rx.lifespan(teardown=teardown):
            raise RuntimeError("app blew up")
    assert ran == [True]


# ---------------------------------------------------------------------------
# Mounting and running  -  what makes brain.py the entry point
# ---------------------------------------------------------------------------


async def test_attach_receptor_binds_and_registers(stack):
    rx = CliReceptor(neuron="echo")
    assert rx.bound is False
    assert stack.orch.attach_receptor(rx) is rx      # returns it, for chaining
    assert rx.bound and rx.dendrite is stack.orch
    assert stack.orch.receptors == [rx]


async def test_attach_receptor_rejects_a_worker(stack):
    # A Receptor on a worker is a wiring mistake - fail at attach, not at
    # the first request.
    with pytest.raises(Exception) as exc:
        stack.worker.attach_receptor(CliReceptor(neuron="echo"))
    assert "orchestrator" in str(exc.value).lower()


async def test_attach_receptor_rejects_a_duplicate(stack):
    rx = CliReceptor(neuron="echo")
    stack.orch.attach_receptor(rx)
    with pytest.raises(ValueError):
        stack.orch.attach_receptor(rx)


async def test_detach_receptor(stack):
    rx = CliReceptor(neuron="echo")
    stack.orch.attach_receptor(rx)
    stack.orch.detach_receptor(rx)
    assert stack.orch.receptors == []
    with pytest.raises(ValueError):
        stack.orch.detach_receptor(rx)


async def test_base_receptor_has_no_transport_to_run(stack):
    rx = _Rx(dendrite=stack.orch, neuron="echo")
    with pytest.raises(NotImplementedError) as exc:
        await rx.run()
    assert "CliReceptor" in str(exc.value)      # points at a usable backend


async def test_run_with_no_receptors_blocks(stack):
    """A brain with no interface is a headless worker node, not an error."""
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(stack.orch.run(), timeout=0.3)


async def test_run_returns_the_finishing_receptors_exit_code(stack):
    rx = CliReceptor(dendrite=stack.orch, neuron="echo", prog="t")

    @rx.command()
    def ask(prompt: str):
        return {"prompt": prompt}

    stack.orch.attach_receptor(rx)
    # argv is the terminal Receptor's - brain.py takes no flags of its own
    assert await rx.run(["hello"]) == 0


async def test_a_finished_interface_does_not_end_the_brain(stack):
    """Rule 2: `:quit` closes a REPL, it does not kill the brain.

    The Receptor is one of four attachments, so one of them finishing must
    leave its siblings serving and the process up. Only a signal ends it,
    which from in here looks like cancellation - hence the wait_for.
    """
    from cosmonapse.receptor.runner import run_receptors

    cancelled = []

    class _Quick(_Rx):
        async def run(self) -> int:
            return 7

    class _Forever(_Rx):
        async def run(self) -> int:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
            return 0

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            run_receptors(_Quick(dendrite=stack.orch),
                          _Forever(dendrite=stack.orch)),
            timeout=0.3,
        )
    # the sibling was still serving when the deadline hit, and only then
    # was it cancelled - not by _Quick returning 7
    assert cancelled == [True]


async def test_every_interface_finishing_idles_rather_than_exiting(stack):
    """Rule 4: nothing left to serve is a headless node, not an exit."""
    from cosmonapse.receptor.runner import run_receptors

    class _Quick(_Rx):
        async def run(self) -> int:
            return 0

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            run_receptors(_Quick(dendrite=stack.orch),
                          _Quick(dendrite=stack.orch)),
            timeout=0.3,
        )


async def test_a_one_shot_invocation_does_end_the_brain(stack):
    """The exception to rule 2: the interface *is* the invocation."""
    from cosmonapse.receptor.runner import run_receptors

    class _OneShot(_Rx):
        async def run(self) -> int:
            self.ends_process = True
            return 7

    class _Forever(_Rx):
        async def run(self) -> int:
            await asyncio.Event().wait()
            return 0

    code = await run_receptors(_OneShot(dendrite=stack.orch),
                               _Forever(dendrite=stack.orch))
    assert code == 7


async def test_cli_marks_a_command_as_ending_the_process_but_not_a_repl(stack):
    """Which of the two a CliReceptor is depends only on argv."""
    rx = CliReceptor(dendrite=stack.orch, neuron="echo", prog="t")

    @rx.command()
    def ask(prompt: str):
        return {"prompt": prompt}

    assert rx.ends_process is False
    assert await rx.run(["ask", "hi"]) == 0
    assert rx.ends_process is True          # one-shot: invocation over

    # --help is a command-line invocation too, so it ends the process
    helped = CliReceptor(dendrite=stack.orch, neuron="echo", prog="t2")
    helped.command()(ask)
    assert await helped.run(["--help"]) == 0
    assert helped.ends_process is True

    # A REPL is the other branch and never sets it. Not exercised here:
    # repl() reads with input() in an executor, which has no stdin under
    # pytest's capture. Rule 2 for long-lived interfaces is covered at the
    # runner level by test_a_finished_interface_does_not_end_the_brain.
    assert CliReceptor(dendrite=stack.orch, neuron="echo").ends_process is False


async def test_a_crashing_receptor_surfaces_not_the_exit_code(stack):
    from cosmonapse.receptor.runner import run_receptors

    class _Boom(_Rx):
        async def run(self) -> int:
            raise RuntimeError("server died")

    class _Forever(_Rx):
        async def run(self) -> int:
            await asyncio.Event().wait()
            return 0

    with pytest.raises(RuntimeError, match="server died"):
        await run_receptors(_Boom(dendrite=stack.orch),
                            _Forever(dendrite=stack.orch))


async def test_http_receptors_sharing_a_port_merge_into_one_app(stack):
    """The property brain.py relies on: /run and /chat on a single server."""
    import httpx
    from fastapi import FastAPI

    from cosmonapse.receptor.api import ApiReceptor
    from cosmonapse.receptor.chat import ChatReceptor

    api = ApiReceptor(dendrite=stack.orch, neuron="echo", path="/run",
                      port=8099, timeout_s=5)
    chat = ChatReceptor(dendrite=stack.orch, neuron="echo", port=8099,
                        timeout_s=5)
    assert api.http_mount()[:2] == ("127.0.0.1", 8099)
    assert chat.http_mount()[:2] == ("127.0.0.1", 8099)

    # Same grouping the runner does, without binding a real socket.
    app = FastAPI()
    for rx in (api, chat):
        app.include_router(rx.http_mount()[2])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://t") as c:
        assert (await c.post("/run", json={"input": "a"})).json()["reply"] \
            == "echo: a"
        assert (await c.post("/chat", json={"message": "b"})).json()["reply"] \
            == "echo: b"
        assert (await c.get("/")).status_code == 200


async def test_a_different_port_splits_them(stack):
    from cosmonapse.receptor.api import ApiReceptor

    a = ApiReceptor(dendrite=stack.orch, neuron="echo", port=8099)
    b = ApiReceptor(dendrite=stack.orch, neuron="echo", port=8100)
    assert a.http_mount()[:2] != b.http_mount()[:2]


async def test_cli_receptor_is_not_http_mounted(stack):
    assert CliReceptor(dendrite=stack.orch, neuron="echo").http_mount() is None


# ---------------------------------------------------------------------------
# Packaging
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_importing_cosmonapse_does_not_import_fastapi():
    import subprocess

    out = subprocess.run(
        [sys.executable, "-c",
         "import sys, cosmonapse; print('fastapi' in sys.modules)"],
        capture_output=True, text=True, check=True,
    )
    assert out.stdout.strip() == "False"


async def test_receptor_names_are_exported():
    import cosmonapse

    for name in ("Receptor", "CliReceptor", "ApiReceptor", "ChatReceptor",
                 "ReceptorError", "ReceptorTimeout", "ReceptorUnbound"):
        assert name in cosmonapse.__all__
        assert getattr(cosmonapse, name) is not None


# ---------------------------------------------------------------------------
# run_brain: the brain-level entry
# ---------------------------------------------------------------------------


async def test_run_brain_starts_every_node_and_serves_their_interfaces():
    """One Dendrite per node, and the brain is what runs.

    Dendrite.run() is Dendrite-scoped, so using it as an entry makes one node
    the thing the process exists for - and with a node per component there is
    no single one to call it on. Note neither node is started by hand here:
    run_brain owns that, and starts every node before serving any interface,
    which is what lets the interface below reach an Axon on the *other* node.
    """
    from cosmonapse import MemoryRegistryStore, run_brain

    synapse = MemorySynapse()
    await synapse.connect()

    worker = Dendrite(synapse=synapse, namespace="brain",
                      dendrite_id="hello-node", role="worker")
    worker.attach_axon(Axon(neuron_id="echo", neuron_fn=echo_neuron,
                            capabilities=["chat"], version="0.0.1"))

    edge = Dendrite(synapse=synapse, namespace="brain",
                    dendrite_id="terminal-node", heartbeat_s=0,
                    registry_store=MemoryRegistryStore())

    seen = []

    class _Probe(_Rx):
        async def run(self) -> int:
            seen.append(await self.ask("hi", timeout_s=5))
            self.ends_process = True
            return 3

    edge.attach_receptor(_Probe(dendrite=edge, neuron="echo"))
    try:
        assert await run_brain(worker, edge) == 3
        assert seen[0]["reply"] == "echo: hi"
    finally:
        await synapse.close()


async def test_run_brain_with_no_interfaces_is_a_headless_brain(stack):
    """Nodes hosting only Axons/Engrams/Effectors mount nothing to serve."""
    from cosmonapse import run_brain

    synapse = MemorySynapse()
    await synapse.connect()
    worker = Dendrite(synapse=synapse, namespace="brain2",
                      dendrite_id="n", role="worker")
    worker.attach_axon(Axon(neuron_id="e", neuron_fn=echo_neuron))
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(run_brain(worker), timeout=0.3)
    finally:
        await synapse.close()


async def test_two_terminal_receptors_warn_about_stdin(stack, caplog):
    """Adding a second CLI interface is a one-click mistake in Genesis."""
    import logging

    from cosmonapse.receptor.runner import _warn_on_contended_stdin

    a = CliReceptor(dendrite=stack.orch, neuron="echo", prog="a")
    b = CliReceptor(dendrite=stack.orch, neuron="echo", prog="b")
    with caplog.at_level(logging.WARNING):
        _warn_on_contended_stdin((a, b))
    assert "race for your input" in caplog.text

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        _warn_on_contended_stdin((a,))
    assert caplog.text == ""
