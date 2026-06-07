"""
Tests for the source-paired Axon factories and their recognisers.

An Axon wraps a Neuron and is the adapter that turns the Neuron's native
output into protocol interactions. These tests cover the recognition half
without any network/provider dependency:

  * the LLM recogniser (``{"response": text}`` -> markers),
  * the MCP recogniser (``is_error`` -> ERROR),
  * end-to-end through ``Axon.handle_task`` with a fake neuron_fn,
  * the source-paired factory wiring (which recogniser each source gets).

Run with:  pytest tests/test_axon_sources.py
(or:  python tests/test_axon_sources.py  -- no pytest required)
"""

import asyncio

from cosmonapse.axon import (
    Axon,
    _extract_cosmo_intent,
    _parse_llm_intents,
    _parse_mcp_intents,
)
from cosmonapse.envelope import Directed, SignalType, task_signal


def _run(coro):
    return asyncio.run(coro)


def _task(input_data=None):
    return task_signal(
        trace_id=None,
        parent_id=None,
        directed=Directed(id="answerer"),
        input=input_data or {"q": "hi"},
    )


# ---------------------------------------------------------------------------
# LLM recogniser
# ---------------------------------------------------------------------------

def test_llm_plain_text_passes_through():
    raw = {"response": "the capital of France is Paris", "meta": {"x": 1}}
    assert _parse_llm_intents(raw) == raw


def test_llm_prose_with_braces_is_not_an_intent():
    raw = {"response": "use the dict {'a': 1} in python"}
    # Not valid JSON / no cosmo key -> stays a plain output.
    assert _parse_llm_intents(raw) is raw


def test_llm_whole_string_clarification_intent():
    raw = {"response": '{"cosmo": "clarification", "question": "which region?"}'}
    out = _parse_llm_intents(raw)
    assert out["__clarification__"] is True
    assert out["question"] == "which region?"


def test_llm_fenced_permission_intent():
    raw = {"response": 'Sure.\n```json\n{"cosmo":"permission","action":"delete","scope":"/db","reason":"cleanup"}\n```'}
    out = _parse_llm_intents(raw)
    assert out["__permission__"] is True
    assert out["action"] == "delete"
    assert out["scope"] == "/db"


def test_llm_error_intent():
    raw = {"response": '{"cosmo":"error","code":"REFUSED","message":"no"}'}
    out = _parse_llm_intents(raw)
    assert out["__error__"] is True
    assert out["code"] == "REFUSED"


def test_llm_output_intent_unwraps():
    raw = {"response": '{"cosmo":"output","output":{"answer":42}}'}
    out = _parse_llm_intents(raw)
    assert out == {"answer": 42}


def test_extract_returns_none_for_non_cosmo_json():
    assert _extract_cosmo_intent('{"foo": "bar"}') is None
    assert _extract_cosmo_intent("just words") is None


# ---------------------------------------------------------------------------
# MCP recogniser
# ---------------------------------------------------------------------------

def test_mcp_is_error_becomes_error_marker():
    raw = {"response": "boom", "is_error": True, "content": "boom", "meta": {}}
    out = _parse_mcp_intents(raw)
    assert out["__error__"] is True
    assert out["code"] == "MCP_TOOL_ERROR"
    assert "boom" in out["message"]


def test_mcp_ok_result_passes_through():
    raw = {"response": "ok", "is_error": False, "result": {"files": 3}}
    assert _parse_mcp_intents(raw) is raw


def test_mcp_can_drive_clarification():
    raw = {"response": '{"cosmo":"clarification","question":"path?"}', "is_error": False}
    out = _parse_mcp_intents(raw)
    assert out["__clarification__"] is True


# ---------------------------------------------------------------------------
# End-to-end through Axon.handle_task
# ---------------------------------------------------------------------------

def _axon_with(parser):
    async def fake_neuron(input, context):
        return fake_neuron.reply
    return Axon(neuron_id="answerer", neuron_fn=fake_neuron,
                output_parser=parser), fake_neuron


def test_handle_task_plain_output():
    axon, fake = _axon_with(_parse_llm_intents)
    fake.reply = {"response": "Paris", "meta": {}}
    sig = _run(axon.handle_task(_task()))
    assert sig.type is SignalType.AGENT_OUTPUT
    assert sig.payload["output"]["response"] == "Paris"


def test_handle_task_clarification():
    axon, fake = _axon_with(_parse_llm_intents)
    fake.reply = {"response": '{"cosmo":"clarification","question":"which?"}'}
    sig = _run(axon.handle_task(_task()))
    assert sig.type is SignalType.CLARIFICATION
    assert sig.payload["question"] == "which?"


def test_handle_task_permission():
    axon, fake = _axon_with(_parse_llm_intents)
    fake.reply = {"response": '{"cosmo":"permission","action":"rm","scope":"/x"}'}
    sig = _run(axon.handle_task(_task()))
    assert sig.type is SignalType.PERMISSION
    assert sig.payload["action"] == "rm"


def test_handle_task_error_marker():
    axon, fake = _axon_with(_parse_mcp_intents)
    fake.reply = {"response": "nope", "is_error": True}
    sig = _run(axon.handle_task(_task()))
    assert sig.type is SignalType.ERROR
    assert sig.payload["code"] == "MCP_TOOL_ERROR"


def test_handle_task_no_parser_is_unchanged():
    # Without a parser the Axon wraps raw output verbatim (back-compat).
    async def fake(input, context):
        return {"response": '{"cosmo":"clarification","question":"x"}'}
    axon = Axon(neuron_id="answerer", neuron_fn=fake)
    sig = _run(axon.handle_task(_task()))
    assert sig.type is SignalType.AGENT_OUTPUT  # marker text not recognised


# ---------------------------------------------------------------------------
# Factory wiring (which recogniser each source gets)
# ---------------------------------------------------------------------------

def test_factory_picks_mcp_recogniser():
    import pytest
    pytest.importorskip("mcp", reason="mcp package not installed")
    axon = Axon.mcp("files", command="echo", args=["hi"])
    assert axon._output_parser is _parse_mcp_intents
    assert axon.neuron_id == "files"


def test_factory_picks_llm_recogniser():
    import pytest
    pytest.importorskip("httpx", reason="httpx not installed")
    axon = Axon.ollama("chat", model="llama3")
    assert axon._output_parser is _parse_llm_intents


def test_factory_recognize_false_disables_parser():
    import pytest
    pytest.importorskip("httpx", reason="httpx not installed")
    axon = Axon.ollama("chat", model="llama3", recognize=False)
    assert axon._output_parser is None


# ---------------------------------------------------------------------------
# Recognition decorators (@axon.detects_clarification, ...)
# ---------------------------------------------------------------------------

def _decorated_axon():
    async def fake(input, context):
        return fake.reply
    axon = Axon(neuron_id="writer", neuron_fn=fake)

    @axon.detects_clarification
    def ask(raw):
        t = raw["response"].strip()
        return {"question": t[4:].strip()} if t.startswith("ASK:") else None

    @axon.detects_permission
    async def perm(raw):  # async detector is supported
        t = raw["response"].strip()
        return {"action": t[5:].strip()} if t.startswith("NEED:") else None

    @axon.detects_output
    def out(raw):
        return {"answer": raw["response"].strip()}

    return axon, fake


def test_decorator_clarification():
    axon, fake = _decorated_axon()
    fake.reply = {"response": "ASK: which region?"}
    sig = _run(axon.handle_task(_task()))
    assert sig.type is SignalType.CLARIFICATION
    assert sig.payload["question"] == "which region?"


def test_decorator_permission_async_detector():
    axon, fake = _decorated_axon()
    fake.reply = {"response": "NEED: delete db"}
    sig = _run(axon.handle_task(_task()))
    assert sig.type is SignalType.PERMISSION
    assert sig.payload["action"] == "delete db"


def test_decorator_output_reshape():
    axon, fake = _decorated_axon()
    fake.reply = {"response": "Paris"}
    sig = _run(axon.handle_task(_task()))
    assert sig.type is SignalType.AGENT_OUTPUT
    assert sig.payload["output"] == {"answer": "Paris"}


def test_decorator_error_precedence():
    axon, fake = _decorated_axon()
    axon.detects_error(lambda raw: {"code": "X", "message": "m"}
                       if "boom" in raw["response"] else None)
    fake.reply = {"response": "ASK: q boom"}  # both error and clarification match
    sig = _run(axon.handle_task(_task()))
    assert sig.type is SignalType.ERROR  # error wins
    assert sig.payload["code"] == "X"


def test_no_recognisers_is_back_compat():
    async def fake(input, context):
        return {"response": "ASK: x"}
    axon = Axon(neuron_id="writer", neuron_fn=fake)  # no decorators, no parser
    sig = _run(axon.handle_task(_task()))
    assert sig.type is SignalType.AGENT_OUTPUT  # nothing recognised -> verbatim


if __name__ == "__main__":
    # Allow running without pytest: execute every test_* in this module.
    import traceback
    g = dict(globals())
    passed = failed = skipped = 0
    for name, fn in sorted(g.items()):
        if not (name.startswith("test_") and callable(fn)):
            continue
        try:
            fn()
            passed += 1
            print(f"PASS {name}")
        except Exception as exc:  # noqa: BLE001
            # importorskip raises a Skipped exception under pytest; without
            # pytest those tests simply error on `import pytest` -> count skip.
            if "importorskip" in traceback.format_exc() or "Skipped" in type(exc).__name__:
                skipped += 1
                print(f"SKIP {name}: {exc}")
            else:
                failed += 1
                print(f"FAIL {name}: {exc}")
    print(f"\n{passed} passed, {failed} failed, {skipped} skipped")
