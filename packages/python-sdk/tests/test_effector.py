"""
tests/test_effector.py
~~~~~~~~~~~~~~~~~~~~~~
Effector barebones tests: Neurons think, Engrams remember, Effectors act.

The test matrix:

* EffectorBinding: construction validation
* Hosted servicing: TOOL_CALL addressed by effector_id / effector_kind
  is invoked and answered with TOOL_RESULT (parent_id == the call's id)
* Failure mapping: a raised invoke() surfaces as TOOL_RESULT ``error``,
  never an ERROR signal
* Non-consumption: a hosted TOOL_CALL still reaches @on_tool_call
  handlers (TOOL_CALL is a PATHWAY_TYPES member, unlike RECALL/IMPRINT)
* Lifecycle: connect() on start, close() on stop / detach
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import pytest_asyncio

from cosmonapse import (
    Axon,
    Dendrite,
    Directed,
    Effector,
    EffectorBinding,
    EffectorNotBound,
    EffectorTimeout,
    MemorySynapse,
    Signal,
    SignalType,
    ToolOutcome,
    new_event_id,
    new_trace_id,
    tool_call_signal,
)
from cosmonapse.effector.standards import (
    parse_claude,
    parse_codex,
    parse_hermes,
)


class EchoEffector(Effector):
    """Toy Effector: echoes args back; ``boom`` raises; ``fail`` errors."""

    def __init__(self, effector_id: str = "fx", effector_kind: str = "toolbox"):
        self.effector_id = effector_id
        self.effector_kind = effector_kind
        self.capabilities = ["echo", "boom", "fail"]
        self.connected = False
        self.closed = False
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.closed = True

    async def invoke(self, tool, args, *, call_id=None, deadline_ms=None,
                     trace_id=None) -> ToolOutcome:
        self.calls.append((tool, args))
        if tool == "boom":
            raise RuntimeError("backend fault")
        if tool == "fail":
            return ToolOutcome(tool=tool, error="tool-level failure",
                               call_id=call_id)
        return ToolOutcome(tool=tool, result={"echo": args},
                           call_id=call_id, effector_id=self.effector_id)


# ---------------------------------------------------------------------------
# EffectorBinding validation
# ---------------------------------------------------------------------------


class TestBinding:

    def test_requires_id_or_type(self):
        with pytest.raises(ValueError, match="directed_id"):
            EffectorBinding(name="fs")

    def test_to_directed(self):
        b = EffectorBinding(name="fs", directed_id="fx", directed_type="toolbox")
        d = b.to_directed()
        assert d.id == "fx" and d.type == "toolbox"


# ---------------------------------------------------------------------------
# Hosted servicing over a MemorySynapse
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def rig():
    syn = MemorySynapse()
    fx = EchoEffector()
    host = Dendrite(
        synapse=syn, namespace="t", dendrite_id="fx-host", role="worker",
        heartbeat_s=0,
    )
    host.attach_effector(fx)

    results: list[Signal] = []
    calls_seen: list[Signal] = []
    caller = Dendrite(
        synapse=syn, namespace="t", dendrite_id="caller",
        role="orchestrator", heartbeat_s=0,
    )
    async def _collect_result(sig: Signal) -> None:
        results.append(sig)

    async def _collect_call(sig: Signal) -> None:
        calls_seen.append(sig)

    caller.on_tool_result(_collect_result)
    caller.on_tool_call(_collect_call)

    await host.start()
    await caller.start()
    try:
        yield syn, fx, host, caller, results, calls_seen
    finally:
        await caller.stop()
        await host.stop()


async def _wait_for(pred, timeout_s: float = 2.0) -> None:
    async def _poll():
        while not pred():
            await asyncio.sleep(0.01)
    await asyncio.wait_for(_poll(), timeout=timeout_s)


def _call(directed: Directed, tool: str, args: dict[str, Any]) -> Signal:
    return tool_call_signal(
        trace_id=new_trace_id(), parent_id=new_event_id(),
        directed=directed, tool=tool, args=args, call_id=new_event_id(),
    )


class TestHostedServicing:

    @pytest.mark.asyncio
    async def test_connect_on_start(self, rig):
        _, fx, *_ = rig
        assert fx.connected

    @pytest.mark.asyncio
    async def test_addressed_by_effector_id(self, rig):
        syn, fx, host, caller, results, _ = rig
        sig = _call(Directed(id="fx"), "echo", {"x": 1})
        await caller._publish(sig)
        await _wait_for(lambda: results)
        r = results[0]
        assert r.type is SignalType.TOOL_RESULT
        assert r.parent_id == sig.id
        assert r.trace_id == sig.trace_id
        assert r.payload["result"] == {"echo": {"x": 1}}
        assert r.payload["call_id"] == sig.payload["call_id"]
        assert r.directed.id == "fx" and r.directed.type == "toolbox"
        assert fx.calls == [("echo", {"x": 1})]

    @pytest.mark.asyncio
    async def test_addressed_by_effector_kind(self, rig):
        syn, fx, host, caller, results, _ = rig
        sig = _call(Directed(type="toolbox"), "echo", {"y": 2})
        await caller._publish(sig)
        await _wait_for(lambda: results)
        assert results[0].payload["result"] == {"echo": {"y": 2}}

    @pytest.mark.asyncio
    async def test_unserved_tool_is_ignored(self, rig):
        syn, fx, host, caller, results, calls_seen = rig
        sig = _call(Directed(id="fx"), "nope", {})
        await caller._publish(sig)
        await _wait_for(lambda: calls_seen)  # call was delivered...
        await asyncio.sleep(0.05)
        assert not results                   # ...but nothing answered
        assert fx.calls == []

    @pytest.mark.asyncio
    async def test_raised_invoke_maps_to_error_result(self, rig):
        syn, fx, host, caller, results, _ = rig
        errors: list[Signal] = []

        async def _collect_error(sig: Signal) -> None:
            errors.append(sig)

        caller.on_error_signal(_collect_error)
        await caller.ensure_subscribed(SignalType.ERROR)
        sig = _call(Directed(id="fx"), "boom", {})
        await caller._publish(sig)
        await _wait_for(lambda: results)
        r = results[0]
        assert r.payload["error"].startswith("effector_exception:")
        assert "result" not in r.payload
        assert not errors  # a tool fault never terminates the TASK

    @pytest.mark.asyncio
    async def test_tool_level_error_rides_result(self, rig):
        syn, fx, host, caller, results, _ = rig
        sig = _call(Directed(id="fx"), "fail", {})
        await caller._publish(sig)
        await _wait_for(lambda: results)
        assert results[0].payload["error"] == "tool-level failure"

    @pytest.mark.asyncio
    async def test_tool_call_not_consumed_by_hosting(self, rig):
        """TOOL_CALL must still reach @on_tool_call handlers (the legacy
        tool-server pattern) even when a hosted Effector services it."""
        syn, fx, host, caller, results, calls_seen = rig
        host_seen: list[Signal] = []

        async def _host_saw(sig: Signal) -> None:
            host_seen.append(sig)

        # Register on the HOST too: servicing and observation coexist.
        host.on_tool_call(_host_saw)
        await host.ensure_subscribed(SignalType.TOOL_CALL)
        sig = _call(Directed(id="fx"), "echo", {"z": 3})
        await caller._publish(sig)
        await _wait_for(lambda: results and host_seen and calls_seen)


class TestLifecycle:

    @pytest.mark.asyncio
    async def test_close_on_stop(self):
        syn = MemorySynapse()
        fx = EchoEffector()
        d = Dendrite(synapse=syn, namespace="t", dendrite_id="h",
                     role="worker", heartbeat_s=0)
        d.attach_effector(fx)
        await d.start()
        await d.stop()
        assert fx.closed

    @pytest.mark.asyncio
    async def test_detach_closes_and_unroutes(self):
        syn = MemorySynapse()
        fx = EchoEffector()
        d = Dendrite(synapse=syn, namespace="t", dendrite_id="h",
                     role="worker", heartbeat_s=0)
        d.attach_effector(fx)
        assert "fx" in d.effectors
        await d.detach_effector("fx")
        assert fx.closed
        assert d.effectors == {}
        assert d._resolve_effector_targets(
            _call(Directed(id="fx"), "echo", {})) == []
        assert d._resolve_effector_targets(
            _call(Directed(type="toolbox"), "echo", {})) == []

    @pytest.mark.asyncio
    async def test_duplicate_attach_rejected(self):
        syn = MemorySynapse()
        d = Dendrite(synapse=syn, namespace="t", dendrite_id="h",
                     role="worker", heartbeat_s=0)
        d.attach_effector(EchoEffector())
        with pytest.raises(ValueError, match="already hosts"):
            d.attach_effector(EchoEffector())


# ---------------------------------------------------------------------------
# Tool-call standards: native dialect parsers
# ---------------------------------------------------------------------------


class TestStandards:

    def test_hermes(self):
        text = ('I will read the file now.\n<tool_call>\n'
                '{"name": "read", "arguments": {"path": "hello.py"}}\n'
                '</tool_call>')
        assert parse_hermes(text) == {
            "tool": "read", "args": {"path": "hello.py"}, "call_id": None,
        }

    def test_claude(self):
        text = ('{"type": "tool_use", "id": "toolu_01", "name": "read", '
                '"input": {"path": "hello.py"}}')
        hit = parse_claude(text)
        assert hit == {
            "tool": "read", "args": {"path": "hello.py"}, "call_id": "toolu_01",
        }
        # fenced variant
        assert parse_claude(f"Sure:\n```json\n{text}\n```") == hit

    def test_codex_tool_calls_array_with_string_args(self):
        text = ('{"tool_calls": [{"id": "call_1", "type": "function", '
                '"function": {"name": "read", '
                '"arguments": "{\\"path\\": \\"hello.py\\"}"}}]}')
        assert parse_codex(text) == {
            "tool": "read", "args": {"path": "hello.py"}, "call_id": "call_1",
        }

    def test_codex_bare_exact_keys(self):
        assert parse_codex('{"name": "read", "arguments": {"path": "x"}}') == {
            "tool": "read", "args": {"path": "x"}, "call_id": None,
        }

    def test_codex_type_marked_variant_accepts_parameters(self):
        # hosted Llamas parrot the advertised schema wrapper: a top-level
        # "type": "function" marker with "parameters" as the args key
        text = ('{"type": "function", "name": "websearch", '
                '"parameters": {"query": "what is k8s"}}')
        assert parse_codex(text) == {
            "tool": "websearch", "args": {"query": "what is k8s"},
            "call_id": None,
        }
        # the Responses-API item shape: type=function_call, string args, id
        text = ('{"type": "function_call", "id": "call_1", "name": "read", '
                '"arguments": "{\"path\": \"x\"}"}')
        assert parse_codex(text) == {
            "tool": "read", "args": {"path": "x"}, "call_id": "call_1",
        }

    def test_codex_schema_echo_with_args_drift(self):
        # the model replays the ENTIRE advertised wrapper and bolts the
        # real args on top - name nested under "function", args top-level
        text = ('{"type": "function", "function": {"name": "websearch", '
                '"description": "Search the web.", "parameters": '
                '{"type": "object", "properties": {"query": '
                '{"type": "string"}}, "required": ["query"]}}, '
                '"arguments": {"query": "what is k8s"}}')
        assert parse_codex(text) == {
            "tool": "websearch", "args": {"query": "what is k8s"},
            "call_id": None,
        }
        # args nested inside the wrapper also count - "arguments" always
        # means args, never a schema
        assert parse_codex(
            '{"type": "function", "function": {"name": "read", '
            '"arguments": {"path": "x"}}}') == {
            "tool": "read", "args": {"path": "x"}, "call_id": None,
        }

    def test_codex_bare_name_parameters_llama_dialect(self):
        # Meta's documented Llama tool format: exactly {"name", "parameters"}
        assert parse_codex('{"name": "websearch", "parameters": '
                           '{"query": "k8s"}}') == {
            "tool": "websearch", "args": {"query": "k8s"}, "call_id": None,
        }

    def test_codex_schema_and_prose_never_misfire(self):
        # any extra key breaks the exact-keys rule
        assert parse_codex('{"name": "report", "parameters": {"q": 1}, '
                           '"notes": "x"}') is None
        # a real advertised schema nests under "function": no top-level
        # name and no true "arguments" key anywhere
        assert parse_codex(
            '{"type": "function", "function": {"name": "read", '
            '"parameters": {"type": "object"}}}') is None

    def test_codex_fenced_call_with_prose_and_trailing_comment(self):
        # narrating models bury the call in prose, bolt a comment onto it,
        # and illustrate a SECOND call later - first call wins
        reply = (
            "I will use the write function.\n"
            "```json\n"
            '{"name": "write", "arguments": {"path": "fib.py", '
            '"content": "print(1)"}}  # written to fib.py\n'
            "```\n"
            "Then you could run it:\n"
            "```json\n"
            '{"name": "bash", "arguments": {"command": "python fib.py"}}\n'
            "```\n")
        assert parse_codex(reply) == {
            "tool": "write",
            "args": {"path": "fib.py", "content": "print(1)"},
            "call_id": None,
        }
        # bare reply with trailing junk after the object
        assert parse_codex('{"name": "ls", "arguments": {}}  # list') == {
            "tool": "ls", "args": {}, "call_id": None,
        }

    def test_prose_embedded_unfenced_object_never_fires(self):
        assert parse_codex(
            'The call {"name": "bash", "arguments": {"command": "rm"}} '
            "would be dangerous.") is None

    def test_ordinary_output_never_misfires(self):
        prose = "The answer is 42, as computed from the tool results."
        json_out = '{"answer": 42, "name": "result", "notes": "arguments were fine"}'
        for parse in (parse_hermes, parse_claude, parse_codex):
            assert parse(prose) is None
            assert parse(json_out) is None
        assert parse_hermes("") is None


# ---------------------------------------------------------------------------
# EffectorClient via dendrite.call_tool
# ---------------------------------------------------------------------------


class TestCallTool:

    @pytest.mark.asyncio
    async def test_call_tool_roundtrip(self, rig):
        syn, fx, host, caller, results, _ = rig
        outcome = await caller.call_tool(
            effector_id="fx", tool="echo", args={"x": 1}, deadline_ms=2000,
        )
        assert outcome.ok
        assert outcome.result == {"echo": {"x": 1}}
        assert outcome.effector_id == "fx"

    @pytest.mark.asyncio
    async def test_call_tool_error_outcome(self, rig):
        syn, fx, host, caller, results, _ = rig
        outcome = await caller.call_tool(
            effector_id="fx", tool="fail", args={}, deadline_ms=2000,
        )
        assert not outcome.ok
        assert outcome.error == "tool-level failure"

    @pytest.mark.asyncio
    async def test_call_tool_timeout(self, rig):
        syn, fx, host, caller, results, _ = rig
        with pytest.raises(EffectorTimeout):
            await caller.call_tool(
                effector_id="nowhere", tool="echo", args={}, deadline_ms=100,
            )


# ---------------------------------------------------------------------------
# The Axon gate: effectors= only with a tool_standard=
# ---------------------------------------------------------------------------


async def _noop_fn(input, context):
    return {"ok": True}


class TestAxonGate:

    def test_effectors_require_tool_standard(self):
        with pytest.raises(ValueError, match="requires tool_standard"):
            Axon(neuron_id="a", neuron_fn=_noop_fn,
                 effectors=[EffectorBinding(name="fx", directed_id="fx")])

    def test_unknown_standard_rejected(self):
        with pytest.raises(ValueError, match="unknown tool_standard"):
            Axon(neuron_id="a", neuron_fn=_noop_fn, tool_standard="qwerty")

    def test_supported_standards(self):
        for std in ("hermes", "claude", "codex", "Hermes"):
            ax = Axon(neuron_id="a", neuron_fn=_noop_fn, tool_standard=std,
                      effectors=[EffectorBinding(name="fx", directed_id="fx")])
            assert ax.tool_standard == std.lower()

    def test_duplicate_binding_names_rejected(self):
        with pytest.raises(ValueError, match="duplicate EffectorBinding"):
            Axon(neuron_id="a", neuron_fn=_noop_fn, tool_standard="hermes",
                 effectors=[EffectorBinding(name="fx", directed_id="a"),
                            EffectorBinding(name="fx", directed_id="b")])

    def test_binding_resolution_precedence(self):
        listed = EffectorBinding(name="box", directed_id="box",
                                 tools=("read", "write"))
        named = EffectorBinding(name="read", directed_id="elsewhere")
        ax = Axon(neuron_id="a", neuron_fn=_noop_fn, tool_standard="hermes",
                  effectors=[named, listed])
        assert ax._resolve_binding_for_tool("read") is listed   # tools= wins
        assert ax._resolve_binding_for_tool("write") is listed
        assert ax._resolve_binding_for_tool("nope") is None     # two bindings, no guess
        solo = Axon(neuron_id="b", neuron_fn=_noop_fn, tool_standard="hermes",
                    effectors=[listed])
        assert solo._resolve_binding_for_tool("anything") is listed

    def test_strict_helper_lookup(self):
        ax = Axon(neuron_id="a", neuron_fn=_noop_fn, tool_standard="hermes",
                  effectors=[EffectorBinding(name="fx", directed_id="fx")])
        with pytest.raises(EffectorNotBound, match="available"):
            ax._resolve_effector_binding("nope")


# ---------------------------------------------------------------------------
# End-to-end: native tool call -> Axon translates -> Effector acts
# ---------------------------------------------------------------------------


HERMES_CALL = ('<tool_call>\n'
               '{"name": "echo", "arguments": {"x": 1}}\n'
               '</tool_call>')


class TestNativeToolFlow:

    async def _run(self, axon, prompt="go"):
        syn = MemorySynapse()
        host = Dendrite(synapse=syn, namespace="e2e", dendrite_id="fx-host",
                        role="worker", heartbeat_s=0)
        host.attach_effector(EchoEffector())
        worker = Dendrite(synapse=syn, namespace="e2e", dendrite_id="worker",
                          role="worker", heartbeat_s=0)
        worker.attach_axon(axon)
        cortex = Dendrite(synapse=syn, namespace="e2e", dendrite_id="cortex",
                          role="orchestrator", heartbeat_s=0)
        await host.start()
        await worker.start()
        await cortex.start()
        try:
            return await cortex.dispatch_and_wait(
                neuron=axon.neuron_id, input={"prompt": prompt},
                timeout_s=3.0,
            )
        finally:
            await cortex.stop()
            await worker.stop()
            await host.stop()

    @pytest.mark.asyncio
    async def test_hermes_call_is_translated_and_acted(self):
        async def llm(input, context):
            return {"response": HERMES_CALL}

        reply = await self._run(Axon(
            neuron_id="agent", neuron_fn=llm, capabilities=["assistant"],
            tool_standard="hermes",
            effectors=[EffectorBinding(name="fx", directed_id="fx",
                                       tools=("echo",),
                                       default_deadline_ms=2000)],
        ))
        assert reply.type is SignalType.AGENT_OUTPUT
        out = reply.payload["output"]
        assert out["tool"] == "echo"
        assert out["args"] == {"x": 1}
        assert out["result"] == {"echo": {"x": 1}}
        assert out["effector_id"] == "fx"

    @pytest.mark.asyncio
    async def test_pure_translation_without_bindings(self):
        async def llm(input, context):
            return {"response": HERMES_CALL}

        reply = await self._run(Axon(
            neuron_id="agent", neuron_fn=llm, capabilities=["assistant"],
            tool_standard="hermes",
        ))
        out = reply.payload["output"]
        assert out == {"tool": "echo", "args": {"x": 1}}  # translated, unexecuted

    @pytest.mark.asyncio
    async def test_unserved_tool_reports_error_in_output(self):
        async def llm(input, context):
            return {"response": ('<tool_call>{"name": "rm_rf", '
                                 '"arguments": {}}</tool_call>')}

        reply = await self._run(Axon(
            neuron_id="agent", neuron_fn=llm, capabilities=["assistant"],
            tool_standard="hermes",
            effectors=[
                EffectorBinding(name="fx", directed_id="fx", tools=("echo",)),
                EffectorBinding(name="other", directed_id="other",
                                tools=("write",)),
            ],
        ))
        assert reply.type is SignalType.AGENT_OUTPUT
        out = reply.payload["output"]
        assert "no effector binding serves tool 'rm_rf'" in out["error"]
        assert "result" not in out

    @pytest.mark.asyncio
    async def test_plain_answer_bypasses_tool_path(self):
        async def llm(input, context):
            return {"response": "The answer is 42."}

        reply = await self._run(Axon(
            neuron_id="agent", neuron_fn=llm, capabilities=["assistant"],
            tool_standard="hermes",
            effectors=[EffectorBinding(name="fx", directed_id="fx")],
        ))
        assert reply.type is SignalType.AGENT_OUTPUT
        assert reply.payload["output"]["response"] == "The answer is 42."

    @pytest.mark.asyncio
    async def test_injected_call_tool_helper(self):
        async def neuron_fn(input, context, *, call_tool):
            outcome = await call_tool("fx", tool="echo", args={"n": 2},
                                      deadline_ms=2000)
            return {"answer": outcome.result}

        reply = await self._run(Axon(
            neuron_id="agent", neuron_fn=neuron_fn,
            capabilities=["assistant"],
            tool_standard="hermes",
            effectors=[EffectorBinding(name="fx", directed_id="fx")],
        ))
        assert reply.type is SignalType.AGENT_OUTPUT
        assert reply.payload["output"]["answer"] == {"echo": {"n": 2}}


# ---------------------------------------------------------------------------
# Effector.serve: the one protocol hook - @on_tool_call -> TOOL_RESULT
# ---------------------------------------------------------------------------


class TestServedEffector:

    @pytest.mark.asyncio
    async def test_return_value_becomes_result(self):
        fx = Effector.serve(effector_id="fx", effector_kind="toolbox")

        @fx.on_tool_call
        async def handle(tool, args, *, call_id):
            return {"served": tool, "args": args, "cid": call_id}

        assert await fx.can_serve("anything")
        out = await fx.invoke("echo", {"x": 1}, call_id="c1")
        assert out.ok
        assert out.result == {"served": "echo", "args": {"x": 1}, "cid": "c1"}
        assert out.effector_id == "fx"

    @pytest.mark.asyncio
    async def test_ordering_fallthrough_and_errors(self):
        fx = Effector.serve(effector_id="fx")
        order = []

        @fx.on_tool_call
        def gate(tool, args):                # sync; None falls through
            order.append("gate")
            return {"blocked": True} if tool == "write" else None

        @fx.on_tool_call
        async def worker(tool, args):
            order.append("worker")
            if tool == "boom":
                raise RuntimeError("backend fault")
            return {"handled": tool}

        out = await fx.invoke("read", {})
        assert out.result == {"handled": "read"}
        assert order == ["gate", "worker"]

        out = await fx.invoke("write", {})
        assert out.result == {"blocked": True}     # first non-None answers

        out = await fx.invoke("boom", {})
        assert not out.ok and out.error == "RuntimeError: backend fault"

    @pytest.mark.asyncio
    async def test_unhandled_and_unregistered(self):
        fx = Effector.serve(effector_id="fx")
        assert not await fx.can_serve("x")          # no handler yet

        @fx.on_tool_call
        async def picky(tool, args):
            return None                             # never answers

        out = await fx.invoke("x", {})
        assert "unhandled tool 'x'" in out.error

    @pytest.mark.asyncio
    async def test_lifecycle_hooks(self):
        fx = Effector.serve(effector_id="fx")
        events = []

        @fx.on_tool_call
        async def handle(tool, args):
            return {"ok": True}

        @fx.on_connect
        async def setup(owner):
            events.append(("connect", owner.effector_id))

        @fx.on_refresh
        def note(owner, event):
            events.append(("refresh", event.reason))

        await fx.connect()
        await fx.refresh(reason="manual-check")
        await fx.close()
        assert events == [("connect", "fx"), ("refresh", "manual-check")]

    @pytest.mark.asyncio
    async def test_on_tool_call_emits_result_on_the_wire(self):
        """@on_tool_call return value rides TOOL_RESULT end to end."""
        fx = Effector.serve(effector_id="proxy")

        @fx.on_tool_call
        async def any_tool(tool, args):
            return {"served": tool}

        syn = MemorySynapse()
        host = Dendrite(synapse=syn, namespace="t", dendrite_id="h",
                        role="worker", heartbeat_s=0)
        host.attach_effector(fx)
        caller = Dendrite(synapse=syn, namespace="t", dendrite_id="c",
                          role="orchestrator", heartbeat_s=0)
        await host.start()
        await caller.start()
        try:
            outcome = await caller.call_tool(
                effector_id="proxy", tool="dynamic-thing", args={},
                deadline_ms=2000,
            )
            assert outcome.ok and outcome.result == {"served": "dynamic-thing"}
        finally:
            await caller.stop()
            await host.stop()
