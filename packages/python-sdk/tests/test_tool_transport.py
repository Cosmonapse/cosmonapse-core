"""
tests/test_tool_transport.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The tool-call TRANSPORT: how a model's request to act travels, as
opposed to how the Axon routes it once recognised (test_effector.py).

The matrix:

* ToolSchema: parameter object, the three provider shapes, building one
  from a Python function, and argument validation
* EffectorBinding.schemas: derives the routing table, type-checked
* Dialect inference from the wired Neuron, always losing to an explicit
  tool_standard=
* The native path: tools= injected into the Neuron's input, structured
  calls read back off ``meta``, and precedence over scraped text
* Multiple calls: reported when not run, run in order when asked for
* Argument validation rejecting a call before it leaves the process
* tool_result_messages: id correlation on the way back
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from cosmonapse import (
    Axon,
    Dendrite,
    Effector,
    EffectorBinding,
    MemorySynapse,
    Neuron,
    ToolOutcome,
    ToolSchema,
    extract_native_calls,
    render_tools,
    tool_result_messages,
    tool_schema,
    validate_args,
)

READ = ToolSchema(
    name="read", description="Read a file.",
    properties={"path": {"type": "string", "description": "relative path"},
                "lines": {"type": "integer"}},
    required=("path",),
)
LS = ToolSchema(name="ls", description="List a directory.",
                properties={"path": {"type": "string"}})


class RecordingEffector(Effector):
    """Records every invocation so a test can assert one did NOT happen."""

    def __init__(self, effector_id: str = "fx"):
        self.effector_id = effector_id
        self.effector_kind = "toolbox"
        self.capabilities = ["read", "ls", "write"]
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def connect(self) -> None: ...
    async def close(self) -> None: ...

    async def can_serve(self, tool: str) -> bool:
        return tool in self.capabilities

    async def invoke(self, tool, args, *, call_id=None, deadline_ms=None,
                     trace_id=None) -> ToolOutcome:
        self.calls.append((tool, args))
        return ToolOutcome(tool=tool, result={"ran": tool, "args": args},
                           call_id=call_id, effector_id=self.effector_id)


async def run_axon(axon, effector=None, prompt="go"):
    """Dispatch one TASK at ``axon`` and return the reply Signal."""
    syn = MemorySynapse()
    fx = effector or RecordingEffector()
    host = Dendrite(synapse=syn, namespace="tt", dendrite_id="fx-host",
                    role="worker", heartbeat_s=0)
    host.attach_effector(fx)
    worker = Dendrite(synapse=syn, namespace="tt", dendrite_id="worker",
                      role="worker", heartbeat_s=0)
    worker.attach_axon(axon)
    cortex = Dendrite(synapse=syn, namespace="tt", dendrite_id="cortex",
                      role="orchestrator", heartbeat_s=0)
    await host.start()
    await worker.start()
    await cortex.start()
    try:
        return await cortex.dispatch_and_wait(
            neuron=axon.neuron_id, input={"prompt": prompt}, timeout_s=3.0,
        )
    finally:
        await cortex.stop()
        await worker.stop()
        await host.stop()


# ---------------------------------------------------------------------------
# ToolSchema
# ---------------------------------------------------------------------------


class TestToolSchema:

    def test_parameters_object(self):
        assert READ.parameters() == {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "relative path"},
                "lines": {"type": "integer"},
            },
            "required": ["path"],
        }

    def test_strict_sets_additional_properties(self):
        s = ToolSchema("x", properties={"a": {"type": "string"}}, strict=True)
        assert s.parameters()["additionalProperties"] is False

    def test_openai_shape(self):
        d = READ.to_openai()
        assert d["type"] == "function"
        assert d["function"]["name"] == "read"
        assert d["function"]["parameters"]["required"] == ["path"]

    def test_anthropic_shape_uses_input_schema(self):
        d = READ.to_anthropic()
        assert d["name"] == "read"
        assert "input_schema" in d and "parameters" not in d

    def test_render_tools_by_dialect(self):
        assert "input_schema" in render_tools([READ], "claude")[0]
        assert render_tools([READ], "hermes")[0]["type"] == "function"
        assert render_tools([READ], "ollama")[0]["type"] == "function"
        # An unknown dialect renders the shape every OpenAI-compatible
        # server understands rather than raising.
        assert render_tools([READ], "nonsense")[0]["type"] == "function"

    def test_required_must_exist_in_properties(self):
        with pytest.raises(ValueError, match="not in properties"):
            ToolSchema("x", properties={"a": {}}, required=("b",))


class TestToolSchemaFromFunction:

    def test_builds_from_signature_and_docstring(self):
        def read(path: str, lines: int = 10, raw: bool = False):
            """Read a file.

            Longer prose that is not part of the summary.
            """
        s = tool_schema(read, params={"path": "relative path"})
        assert s.name == "read"
        assert s.description == "Read a file."
        assert s.required == ("path",)
        assert s.properties["path"] == {"type": "string",
                                        "description": "relative path"}
        assert s.properties["lines"]["type"] == "integer"
        assert s.properties["raw"]["type"] == "boolean"

    def test_optional_and_unannotated(self):
        def f(a, b: str | None = None):
            ...
        s = tool_schema(f)
        assert "type" not in s.properties["a"]      # unannotated: unconstrained
        assert s.properties["b"]["type"] == "string"  # Optional[str] -> string
        assert s.required == ("a",)

    def test_varargs_and_self_skipped(self):
        def f(self, a: str, *args, **kwargs):
            ...
        assert set(tool_schema(f).properties) == {"a"}


class TestValidateArgs:

    def test_accepts_good_args(self):
        assert validate_args(READ, {"path": "a.py", "lines": 3}) is None

    def test_missing_required(self):
        msg = validate_args(READ, {"lines": 3})
        assert msg is not None and "missing required" in msg and "path" in msg

    def test_wrong_type_named(self):
        msg = validate_args(READ, {"path": "a", "lines": "three"})
        assert msg is not None and "'lines'" in msg and "integer" in msg

    def test_bool_is_not_an_integer(self):
        # True is an int in Python; accepting it silently runs the tool
        # with an argument the model did not mean.
        assert validate_args(READ, {"path": "a", "lines": True}) is not None

    def test_unknown_key_ignored_unless_strict(self):
        assert validate_args(READ, {"path": "a", "extra": 1}) is None
        strict = ToolSchema("read", properties={"path": {"type": "string"}},
                            required=("path",), strict=True)
        assert "unknown argument" in (
            validate_args(strict, {"path": "a", "extra": 1}) or "")

    def test_enum_membership(self):
        s = ToolSchema("m", properties={"mode": {"type": "string",
                                                 "enum": ["r", "w"]}})
        assert validate_args(s, {"mode": "r"}) is None
        assert "must be one of" in (validate_args(s, {"mode": "x"}) or "")


# ---------------------------------------------------------------------------
# EffectorBinding.schemas
# ---------------------------------------------------------------------------


class TestBindingSchemas:

    def test_schemas_derive_the_routing_table(self):
        b = EffectorBinding(name="fs", directed_id="fx", schemas=(READ, LS))
        assert b.tools == ("read", "ls")
        assert b.schema_for("read") is READ
        assert b.schema_for("nope") is None

    def test_explicit_tools_wins(self):
        b = EffectorBinding(name="fs", directed_id="fx", schemas=(READ,),
                            tools=("read", "extra"))
        assert b.tools == ("read", "extra")

    def test_rejects_non_schema(self):
        with pytest.raises(TypeError, match="ToolSchema instances"):
            EffectorBinding(name="fs", directed_id="fx",
                            schemas=({"name": "read"},))

    def test_no_schemas_changes_nothing(self):
        b = EffectorBinding(name="fs", directed_id="fx", tools=("read",))
        assert b.schemas == () and b.tools == ("read",)


# ---------------------------------------------------------------------------
# Dialect inference
# ---------------------------------------------------------------------------


class TestInference:

    def _axon(self, fn, **kw):
        return Axon(neuron_id="a", neuron_fn=fn, **kw)

    def test_anthropic_infers_claude(self):
        n = Neuron(source="anthropic", model="claude-sonnet-4-6", api_key="k")
        assert self._axon(n).tool_standard == "claude"

    def test_qwen_infers_hermes(self):
        n = Neuron(source="ollama", model="qwen2.5-coder:7b")
        assert self._axon(n).tool_standard == "hermes"

    def test_gpt_infers_codex(self):
        n = Neuron(source="openai", model="gpt-4o", api_key="k")
        assert self._axon(n).tool_standard == "codex"

    def test_unknown_model_falls_back_to_auto(self):
        n = Neuron(source="ollama", model="some-private-finetune")
        assert self._axon(n).tool_standard == "auto"

    def test_plain_callable_infers_nothing(self):
        async def fn(input, context):
            return {}
        assert self._axon(fn).tool_standard is None

    def test_explicit_always_wins(self):
        n = Neuron(source="ollama", model="qwen2.5-coder:7b")
        assert self._axon(n, tool_standard="codex").tool_standard == "codex"

    def test_effectors_still_require_a_recognisable_dialect(self):
        # The construction gate is intact where nothing can be inferred:
        # bindings that could never be reached still fail loudly.
        async def fn(input, context):
            return {}
        with pytest.raises(ValueError, match="requires tool_standard"):
            self._axon(fn, effectors=[EffectorBinding(name="fx",
                                                      directed_id="fx")])

    def test_auto_is_a_valid_explicit_standard(self):
        async def fn(input, context):
            return {}
        assert self._axon(fn, tool_standard="auto").tool_standard == "auto"


# ---------------------------------------------------------------------------
# The native path
# ---------------------------------------------------------------------------


class TestNativeTransport:

    def test_no_schemas_means_no_payload(self):
        async def fn(input, context):
            return {}
        ax = Axon(neuron_id="a", neuron_fn=fn, tool_standard="hermes",
                  effectors=[EffectorBinding(name="fx", directed_id="fx",
                                             tools=("read",))])
        assert ax.native_tools is None

    def test_payload_is_rendered_in_the_provider_shape(self):
        anthropic = Axon(
            neuron_id="a",
            neuron_fn=Neuron(source="anthropic", model="claude-sonnet-4-6",
                             api_key="k"),
            effectors=[EffectorBinding(name="fx", directed_id="fx",
                                       schemas=(READ,))],
        )
        assert "input_schema" in anthropic.native_tools[0]

        openai = Axon(
            neuron_id="a",
            neuron_fn=Neuron(source="openai", model="gpt-4o", api_key="k"),
            effectors=[EffectorBinding(name="fx", directed_id="fx",
                                       schemas=(READ,))],
        )
        assert openai.native_tools[0]["function"]["name"] == "read"

    @pytest.mark.asyncio
    async def test_tools_reach_the_neuron_input(self):
        seen: dict[str, Any] = {}

        async def fn(input, context):
            seen.update(input)
            return {"response": "done"}

        await run_axon(Axon(
            neuron_id="a", neuron_fn=fn, tool_standard="codex",
            effectors=[EffectorBinding(name="fx", directed_id="fx",
                                       schemas=(READ,))],
        ))
        assert seen["tools"][0]["function"]["name"] == "read"

    @pytest.mark.asyncio
    async def test_no_tools_key_injected_without_schemas(self):
        seen: dict[str, Any] = {}

        async def fn(input, context):
            seen.update(input)
            return {"response": "done"}

        await run_axon(Axon(
            neuron_id="a", neuron_fn=fn, tool_standard="codex",
            effectors=[EffectorBinding(name="fx", directed_id="fx",
                                       tools=("read",))],
        ))
        assert "tools" not in seen

    @pytest.mark.asyncio
    async def test_structured_call_off_meta_is_dispatched(self):
        # The reply carries NO parseable text call - only the provider's
        # own tool channel, which is the whole point of the native path.
        async def fn(input, context):
            return {"response": "", "meta": {"choices": [{"message": {
                "tool_calls": [{"id": "call_1", "type": "function",
                                "function": {"name": "read",
                                             "arguments": '{"path": "a.py"}'}}],
            }}]}}

        reply = await run_axon(Axon(
            neuron_id="a", neuron_fn=fn, tool_standard="codex",
            effectors=[EffectorBinding(name="fx", directed_id="fx",
                                       schemas=(READ,))],
        ))
        out = reply.payload["output"]
        assert out["tool"] == "read"
        assert out["args"] == {"path": "a.py"}
        assert out["call_id"] == "call_1"
        assert out["result"] == {"ran": "read", "args": {"path": "a.py"}}

    @pytest.mark.asyncio
    async def test_anthropic_tool_use_block_is_reachable(self):
        # The block _AnthropicNeuron filters out of ``response``; it
        # survives on ``meta``, which is where it is now read from.
        async def fn(input, context):
            return {"response": "Let me look.", "meta": {"content": [
                {"type": "text", "text": "Let me look."},
                {"type": "tool_use", "id": "toolu_1", "name": "read",
                 "input": {"path": "a.py"}},
            ]}}

        reply = await run_axon(Axon(
            neuron_id="a", neuron_fn=fn, tool_standard="claude",
            effectors=[EffectorBinding(name="fx", directed_id="fx",
                                       schemas=(READ,))],
        ))
        assert reply.payload["output"]["call_id"] == "toolu_1"

    @pytest.mark.asyncio
    async def test_structured_beats_narrated_text(self):
        async def fn(input, context):
            return {
                "response": '<tool_call>{"name":"ls","arguments":{}}</tool_call>',
                "meta": {"content": [{"type": "tool_use", "id": "t1",
                                      "name": "read",
                                      "input": {"path": "a.py"}}]},
            }

        reply = await run_axon(Axon(
            neuron_id="a", neuron_fn=fn, tool_standard="hermes",
            effectors=[EffectorBinding(name="fx", directed_id="fx",
                                       schemas=(READ, LS))],
        ))
        assert reply.payload["output"]["tool"] == "read"

    def test_extract_native_accepts_a_bare_provider_payload(self):
        assert extract_native_calls(
            {"message": {"tool_calls": [
                {"function": {"name": "read", "arguments": {"path": "a"}}}]}}
        ) == [{"tool": "read", "args": {"path": "a"}, "call_id": None}]


# ---------------------------------------------------------------------------
# More than one call
# ---------------------------------------------------------------------------


PARALLEL = {"response": "", "meta": {"choices": [{"message": {"tool_calls": [
    {"id": "c1", "type": "function",
     "function": {"name": "read", "arguments": '{"path": "a.py"}'}},
    {"id": "c2", "type": "function",
     "function": {"name": "ls", "arguments": "{}"}},
]}}]}}


class TestMultipleCalls:

    def _axon(self, **kw):
        async def fn(input, context):
            return PARALLEL
        return Axon(neuron_id="a", neuron_fn=fn, tool_standard="codex",
                    effectors=[EffectorBinding(name="fx", directed_id="fx",
                                               schemas=(READ, LS))], **kw)

    @pytest.mark.asyncio
    async def test_unrun_calls_are_reported_not_dropped(self):
        fx = RecordingEffector()
        reply = await run_axon(self._axon(), effector=fx)
        out = reply.payload["output"]
        assert out["tool"] == "read"                 # first still on top
        assert out["dropped_calls"] == [
            {"tool": "ls", "args": {}, "call_id": "c2"},
        ]
        assert [c[0] for c in fx.calls] == ["read"]  # only the first ran

    @pytest.mark.asyncio
    async def test_parallel_tools_runs_all_in_order(self):
        fx = RecordingEffector()
        reply = await run_axon(self._axon(parallel_tools=True), effector=fx)
        out = reply.payload["output"]
        assert out["tool"] == "read"                 # back-compat top level
        assert [c["tool"] for c in out["calls"]] == ["read", "ls"]
        assert [c[0] for c in fx.calls] == ["read", "ls"]
        assert "dropped_calls" not in out

    @pytest.mark.asyncio
    async def test_single_call_has_neither_key(self):
        async def fn(input, context):
            return {"response": '{"name": "ls", "arguments": {}}'}
        reply = await run_axon(Axon(
            neuron_id="a", neuron_fn=fn, tool_standard="codex",
            effectors=[EffectorBinding(name="fx", directed_id="fx",
                                       schemas=(LS,))],
        ))
        out = reply.payload["output"]
        assert "dropped_calls" not in out and "calls" not in out


# ---------------------------------------------------------------------------
# Validation at the boundary
# ---------------------------------------------------------------------------


class TestArgumentValidation:

    @pytest.mark.asyncio
    async def test_bad_args_never_reach_the_effector(self):
        fx = RecordingEffector()

        async def fn(input, context):
            return {"response": '{"name": "read", "arguments": {"lines": 3}}'}

        reply = await run_axon(Axon(
            neuron_id="a", neuron_fn=fn, tool_standard="codex",
            effectors=[EffectorBinding(name="fx", directed_id="fx",
                                       schemas=(READ,))],
        ), effector=fx)
        out = reply.payload["output"]
        assert "missing required" in out["error"]
        assert "result" not in out
        assert fx.calls == []           # the tool never ran

    @pytest.mark.asyncio
    async def test_undeclared_tool_is_not_validated(self):
        # A binding with no schema for the tool keeps the old behaviour:
        # dispatch, and let the Effector decide.
        fx = RecordingEffector()

        async def fn(input, context):
            return {"response": '{"name": "write", "arguments": {"x": 1}}'}

        reply = await run_axon(Axon(
            neuron_id="a", neuron_fn=fn, tool_standard="codex",
            effectors=[EffectorBinding(name="fx", directed_id="fx",
                                       schemas=(READ,), tools=("read",
                                                               "write"))],
        ), effector=fx)
        assert reply.payload["output"]["result"]["ran"] == "write"
        assert fx.calls == [("write", {"x": 1})]


# ---------------------------------------------------------------------------
# Feeding the observation back
# ---------------------------------------------------------------------------


class TestToolResultMessages:

    EX: ClassVar[list[dict[str, Any]]] = [
        {"tool": "read", "args": {"path": "a.py"}, "call_id": "call_1",
         "result": {"content": "hi"}},
        {"tool": "ls", "args": {}, "call_id": None, "error": "no such dir"},
    ]

    def test_openai_pairs_every_result_to_its_call(self):
        msgs = tool_result_messages(self.EX, "openai")
        assistant, *tools = msgs
        ids = [c["id"] for c in assistant["tool_calls"]]
        assert len(set(ids)) == 2
        assert [m["tool_call_id"] for m in tools] == ids
        assert all(m["role"] == "tool" for m in tools)

    def test_synthesised_id_cannot_collide_with_a_real_one(self):
        # exchange 2 has no id and would naturally be numbered "call_1",
        # which exchange 1 already owns.
        ids = [c["id"] for c in
               tool_result_messages(self.EX, "openai")[0]["tool_calls"]]
        assert ids[0] == "call_1" and ids[1] != "call_1"

    def test_anthropic_groups_results_in_one_user_turn(self):
        assistant, user = tool_result_messages(self.EX, "anthropic")
        assert assistant["role"] == "assistant"
        assert [b["type"] for b in assistant["content"]] == ["tool_use"] * 2
        assert user["role"] == "user"
        assert len(user["content"]) == 2
        assert user["content"][1]["is_error"] is True

    def test_hermes_text_is_the_chatml_pair(self):
        msgs = tool_result_messages(self.EX[:1], "hermes_text")
        assert "<tool_call>" in msgs[0]["content"]
        assert "<tool_response>" in msgs[1]["content"]

    def test_ollama_sends_object_args_and_no_ids(self):
        assistant, *tools = tool_result_messages(self.EX, "ollama")
        assert assistant["tool_calls"][0]["function"]["arguments"] == {
            "path": "a.py"}
        assert "tool_call_id" not in tools[0]

    def test_empty_is_empty(self):
        assert tool_result_messages([], "openai") == []
