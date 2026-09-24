"""Glia: the card itself - vocabulary, scope, artifact, modes, journal.

A card is a two-way gate around one component. Every policy has a scope
(``direction`` and ``types``, default every signal both ways), a verdict,
and a ``retry`` for what a violation does next. There are no checkpoints.
"""

from __future__ import annotations

import json

import pytest

import cosmonapse as cn
from cosmonapse import Directed, SignalType, task_signal
from cosmonapse.glia import (
    BUS_FIELDS,
    Decision,
    Direction,
    Glia,
    Journal,
    Mode,
    PolicyArtifact,
    PolicyError,
    RepairPolicy,
    Retry,
    SignalView,
    TraceCounter,
    TraceLimits,
    Verdict,
    allowed_retry,
    describe,
    is_exempt,
    mark_exempt,
    redact_text_leaves,
)

IN = Direction.INBOUND
OUT = Direction.OUTBOUND


def card(**over):
    body = {"card_id": "t", "version": "1", "mode": "enforce", "policies": []}
    body.update(over)
    return Glia.load(json.dumps(body))


def a_task(**inp):
    return task_signal(directed=Directed(id="n"), input=inp)


def an_output(**out):
    return cn.agent_output_signal(
        trace_id=cn.new_trace_id(), parent_id=cn.new_event_id(),
        directed=Directed(id="n"), output=out,
    )


def a_tool_call(tool="shell", **args):
    return cn.tool_call_signal(
        trace_id=cn.new_trace_id(), parent_id=cn.new_event_id(),
        directed=Directed(id="fx1"), tool=tool, args=args,
    )


def an_imprint(op="add", **entry):
    return cn.imprint_signal(
        trace_id=cn.new_trace_id(), parent_id=cn.new_event_id(),
        directed=Directed(id="eg1"), op=op, entry=entry,
    )


# ---------------------------------------------------------------------------
# Vocabulary and introspection
# ---------------------------------------------------------------------------


def test_there_are_no_checkpoints_only_scope():
    d = describe()
    assert "checkpoints" not in d
    assert d["directions"] == ["inbound", "outbound", "both"]
    assert d["default_direction"] == "both"
    assert set(d["signal_types"]) == {t.value for t in SignalType}
    assert d["retry"] == ["reask", "resend", "none"]
    assert d["default_retry"] == "none"
    assert d["verdicts"] == ["allow", "deny", "redact", "escalate"]
    assert d["modes"] == ["off", "audit", "enforce"]
    assert d["default_mode"] == "off"


def test_introspection_is_enough_to_build_a_card_editor():
    d = describe()
    # Genesis must not have to hand-mirror these.
    for key in ("rule_fields", "card_fields", "limit_modes", "meta_key",
                "bus_fields", "escalatable", "on_exceed", "resendable",
                "gates"):
        assert d[key], key
    for field in ("id", "direction", "types", "verdict", "retry",
                  "max_attempts"):
        assert field in d["rule_fields"], field
    assert "checkpoint" not in d["rule_fields"]
    assert d["gates"]["axon"]["inbound"][0] == "TASK"
    assert d["gates"]["effector"] == {
        "inbound": ["TOOL_CALL"], "outbound": ["TOOL_RESULT"],
    }


def test_resend_is_only_offered_where_the_request_is_safe_to_repeat():
    assert set(describe()["resendable"]) == {"TOOL_RESULT", "RECALLED"}


def test_trace_strict_is_documented_as_not_built():
    assert "trace_strict" in describe()["not_built"]
    assert "trace_strict" not in describe()["limit_modes"]


def test_there_is_no_centralized_flag_anywhere():
    import cosmonapse.glia.artifact as art
    import cosmonapse.glia.base as base

    for mod in (base, art):
        assert not any(
            "centraliz" in name.lower() for name in dir(mod)
        ), mod.__name__


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


async def test_a_policy_with_no_scope_reads_every_signal_both_ways():
    c = card(policies=[
        {"id": "all", "verdict": "deny", "match": ["forbidden"]},
    ])
    for sig in (a_task(q="forbidden"), an_output(t="forbidden"),
                a_tool_call(cmd="forbidden"), an_imprint(v="forbidden")):
        for d in (IN, OUT):
            oc = await c.check(sig, direction=d)
            assert oc is not None and oc.denied, (sig.type, d)


async def test_direction_narrows_the_scope():
    c = card(policies=[
        {"id": "in-only", "direction": "inbound", "verdict": "deny",
         "always": True},
    ])
    assert (await c.check(a_task(), direction=IN)).denied
    assert await c.check(a_task(), direction=OUT) is None


async def test_types_narrow_the_scope():
    c = card(policies=[
        {"id": "calls", "types": ["TOOL_CALL"], "verdict": "deny",
         "always": True},
    ])
    assert (await c.check(a_tool_call(), direction=OUT)).denied
    assert (await c.check(a_tool_call(), direction=IN)).denied
    assert await c.check(a_task(), direction=IN) is None


def test_types_accept_a_star_and_reject_an_unknown_name():
    c = card(policies=[
        {"id": "s", "types": ["*"], "verdict": "deny", "always": True},
    ])
    assert c.declares(IN, SignalType.HEARTBEAT)
    with pytest.raises(PolicyError, match="unknown signal type"):
        card(policies=[
            {"id": "x", "types": ["TOOLCALL"], "verdict": "deny",
             "always": True},
        ])


def test_declares_is_the_null_check():
    c = card(policies=[
        {"id": "r", "direction": "inbound", "types": ["TASK"],
         "verdict": "deny", "always": True},
    ])
    assert c.declares(IN, SignalType.TASK)
    assert not c.declares(OUT, SignalType.TASK)
    assert not c.declares(IN, SignalType.RECALL)


async def test_a_signal_no_policy_selects_is_not_checked():
    c = card(policies=[
        {"id": "r", "direction": "inbound", "types": ["TASK"],
         "verdict": "deny", "always": True},
    ])
    assert await c.check(an_imprint(), direction=IN) is None
    assert c.counters["checked"] == 0


# ---------------------------------------------------------------------------
# Mode
# ---------------------------------------------------------------------------


async def test_mode_defaults_to_off_and_off_reads_nothing():
    c = Glia(card_id="x")
    assert c.mode is Mode.OFF
    assert c.active is False

    @c.on_signal
    def never(sig):  # pragma: no cover - must not run
        raise AssertionError("a card in mode=off must not read a signal")

    assert await c.check(a_task(a=1), direction=IN) is None


async def test_audit_records_a_shadow_verdict_and_blocks_nothing():
    c = card(mode="audit", policies=[
        {"id": "d", "verdict": "deny", "always": True, "reason": "no"},
    ])
    oc = await c.check(a_task(a=1), direction=IN)
    assert oc.effective is Decision.ALLOW
    assert oc.applied is False
    assert oc.record["verdict"] == "would_deny"
    assert oc.record["direction"] == "inbound"
    assert oc.record["signal"] == "TASK"


async def test_enforce_applies_the_verdict():
    c = card(policies=[
        {"id": "d", "verdict": "deny", "always": True, "reason": "no"},
    ])
    oc = await c.check(a_task(a=1), direction=IN)
    assert oc.denied and oc.applied and oc.reason == "no"
    assert oc.scope == "inbound:TASK"


async def test_the_deployment_can_set_the_mode_by_env(monkeypatch):
    monkeypatch.setenv("COSMONAPSE_POLICY_MODE", "audit")
    assert card(mode="enforce").mode is Mode.AUDIT


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def test_handlers_run_in_order_and_first_non_none_answers():
    c = Glia(card_id="h", mode="enforce")
    seen = []

    @c.on_signal(types=["TOOL_CALL"])
    def first(sig):
        # None falls through to the next handler.
        seen.append("first")

    @c.on_signal(types=["TOOL_CALL"])
    async def second(sig):
        seen.append("second")
        return Verdict.deny("second answered")

    @c.on_signal(types=["TOOL_CALL"])
    def third(sig):  # pragma: no cover - must not be reached
        raise AssertionError("a handler after the answer must not run")

    oc = await c.check(a_tool_call(), direction=OUT)
    assert seen == ["first", "second"]
    assert oc.reason == "second answered"


async def test_a_handler_outside_its_scope_does_not_run():
    c = Glia(card_id="h", mode="enforce")

    @c.on_signal(direction="outbound", types=["IMPRINT"])
    def only_writes(sig):  # pragma: no cover - out of scope
        raise AssertionError("an out-of-scope handler must not run")

    assert await c.check(an_imprint(), direction=IN) is None
    assert await c.check(a_task(), direction=OUT) is None


async def test_a_handler_sees_a_read_only_view_of_the_whole_signal():
    c = Glia(card_id="h", mode="enforce")
    got = {}

    @c.on_signal
    def h(sig, *, trace_id=None, component=None, direction=None, card=None):
        got.update(view=sig, trace_id=trace_id, component=component,
                   direction=direction, card=card)

    sig = cn.tool_result_signal(
        trace_id=cn.new_trace_id(), parent_id=cn.new_event_id(),
        directed=Directed(id="fx1", type="shell"), tool="shell",
        result={"ok": True}, meta={"x": {"y": 1}},
    )
    await c.check(sig, direction=IN, component="n1")
    view = got["view"]
    assert isinstance(view, SignalView)
    assert view.type is SignalType.TOOL_RESULT
    assert view.directed_id == "fx1" and view.directed_type == "shell"
    assert view.parent_id == sig.parent_id and view.trace_id == sig.trace_id
    assert view.payload == sig.payload
    assert view.direction is IN and view.component == "n1"
    assert got["trace_id"] == sig.trace_id
    assert got["component"] == "n1"
    assert got["direction"] is IN
    assert got["card"] is c
    # Read-only: the view cannot rewrite the signal.
    with pytest.raises(TypeError):
        view.meta["x"] = 2
    view.payload["tool"] = "tampered"
    assert sig.payload["tool"] == "shell", "the view's payload is a copy"


async def test_a_raising_handler_fails_closed_by_default():
    c = Glia(card_id="h", mode="enforce")

    @c.on_signal
    def boom(sig):
        raise RuntimeError("policy bug")

    oc = await c.check(a_task(), direction=IN)
    assert oc.denied
    assert c.counters["handler_failed"] == 1


async def test_fail_open_is_the_explicit_per_card_opt_out():
    c = Glia(card_id="h", mode="enforce", fail_open=True)

    @c.on_signal
    def boom(sig):
        raise RuntimeError("policy bug")

    oc = await c.check(a_task(), direction=IN)
    assert oc.effective is Decision.ALLOW
    # The failure is recorded either way.
    assert c.counters["handler_failed"] == 1


async def test_a_handler_returning_a_non_verdict_fails_closed():
    c = Glia(card_id="h", mode="enforce")

    @c.on_signal
    def wrong(sig):
        return "deny"

    assert (await c.check(a_task(), direction=IN)).denied


async def test_a_handler_redacting_with_a_non_payload_fails_closed():
    c = Glia(card_id="h", mode="enforce")

    @c.on_signal
    def wrong(sig):
        return Verdict.redact("not a payload")

    assert (await c.check(a_task(), direction=IN)).denied


async def test_a_handler_carries_its_retry_and_budget():
    c = Glia(card_id="h", mode="enforce")

    @c.on_signal(direction="inbound", types=["RECALLED"], retry="resend",
                 max_attempts=3)
    def no(sig):
        return Verdict.deny("stale memory")

    sig = cn.recalled_signal(
        trace_id=cn.new_trace_id(), parent_id=cn.new_event_id(),
        engram_id="eg1", hits=[],
    )
    oc = await c.check(sig, direction=IN)
    assert oc.retry is Retry.RESEND and oc.max_attempts == 3
    assert oc.record["retry"] == "resend"


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def test_redaction_returns_a_copy_and_never_mutates():
    original = {"a": "sk-ABCDEF123456", "nested": [{"b": "sk-ZZZZZZ999999"}]}
    snapshot = json.dumps(original, sort_keys=True)
    import re
    out, subs = redact_text_leaves(
        original, (re.compile(r"sk-[A-Z0-9]{6,}"),), "[x]",
    )
    assert subs == 2
    assert out["a"] == "[x]"
    # The input is byte-identical: MemorySynapse hands the same Signal
    # object to every in-process subscriber.
    assert json.dumps(original, sort_keys=True) == snapshot


async def test_an_artifact_redact_rule_rewrites_a_copy_of_the_payload():
    c = card(policies=[
        {"id": "keys", "verdict": "redact",
         "match": [r"sk-[A-Za-z0-9]{6,}"]},
    ])
    sig = an_output(text="take sk-ABC123456 please")
    oc = await c.check(sig, direction=OUT)
    assert oc.redacted
    assert oc.replacement == {"output": {"text": "take [redacted] please"}}
    assert sig.payload == {"output": {"text": "take sk-ABC123456 please"}}


async def test_a_keys_rule_fires_on_a_nested_key_and_drops_it():
    c = card(policies=[
        {"id": "ssn", "types": ["IMPRINT"], "verdict": "redact",
         "keys": ["ssn"]},
    ])
    oc = await c.check(an_imprint(ssn="1", name="ada"), direction=IN)
    assert oc.replacement["entry"] == {"name": "ada"}
    assert oc.replacement["op"] == "add"


# ---------------------------------------------------------------------------
# The bus record and the journal
# ---------------------------------------------------------------------------


async def test_the_bus_record_carries_no_matched_content(tmp_path):
    jpath = tmp_path / "p.jsonl"
    c = card(journal={"path": str(jpath)}, policies=[
        {"id": "keys", "direction": "outbound", "verdict": "deny",
         "match": [r"sk-[A-Za-z0-9]{6,}"], "reason": "no keys out"},
    ])
    oc = await c.check(an_output(t="sk-SUPERSECRET1"), direction=OUT,
                       component="n1")
    blob = json.dumps(oc.record)
    assert "SUPERSECRET" not in blob
    assert set(oc.record) <= BUS_FIELDS
    assert oc.record["component"] == "n1"
    # ... and the matched text is in the local journal, and only there.
    entries = Journal(jpath).read_all()
    assert len(entries) == 1
    assert entries[0]["matched"] == "sk-SUPERSECRET1"
    assert entries[0]["hash"] == oc.record["hash"]
    assert entries[0]["direction"] == "outbound"
    assert entries[0]["signal"] == "AGENT_OUTPUT"


async def test_a_pass_writes_no_record_unless_sampled():
    rule = {"id": "keys", "verdict": "deny", "match": ["never-matches-this"]}
    c = card(policies=[rule])
    oc = await c.check(an_output(t="fine"), direction=OUT)
    assert oc.effective is Decision.ALLOW
    assert oc.record == {}

    c2 = card(sample_pass=1, policies=[rule])
    rec = (await c2.check(an_output(t="fine"), direction=OUT)).record
    assert rec["verdict"] == "allow"


def test_the_journal_is_append_only_jsonl(tmp_path):
    j = Journal(tmp_path / "a" / "b.jsonl")
    j.record({"one": 1})
    j.record({"two": 2})
    assert [next(iter(e)) for e in j.read_all()] == ["one", "two"]
    assert j.written == 2
    assert j.describe()["kind"] == "jsonl"


def test_a_journal_that_cannot_be_written_does_not_raise(tmp_path):
    j = Journal(tmp_path)  # a directory, so the open fails
    j.record({"a": 1})
    assert j.failed == 1


# ---------------------------------------------------------------------------
# Gate-generated signals and the audit stream pass unread
# ---------------------------------------------------------------------------


async def test_a_gate_generated_signal_passes_every_gate_unread():
    c = card(policies=[{"id": "all", "verdict": "deny", "always": True}])
    refusal = mark_exempt(an_output(error="refused"))
    assert is_exempt(refusal)
    assert await c.check(refusal, direction=OUT) is None


async def test_an_audit_record_is_never_gated():
    c = card(policies=[{"id": "all", "verdict": "deny", "always": True}])
    audit = cn.audit_signal(
        trace_id=cn.new_trace_id(), parent_id=cn.new_event_id(),
        kind="GUARDED", domain="security", outcome="refused",
    )
    assert await c.check(audit, direction=OUT) is None


# ---------------------------------------------------------------------------
# The artifact
# ---------------------------------------------------------------------------


def test_the_artifact_is_content_addressed_and_reports_its_version():
    a = PolicyArtifact.parse(json.dumps({"card_id": "x", "version": "9"}))
    b = PolicyArtifact.parse(json.dumps({"card_id": "x", "version": "9"}))
    assert a.content_hash == b.content_hash
    assert a.to_card().version == "9"


def test_a_card_loads_from_an_env_var(monkeypatch):
    monkeypatch.setenv("MY_CARD", json.dumps({"card_id": "env", "version": "2"}))
    assert Glia.load("MY_CARD").card_id == "env"


def test_a_card_loads_from_a_file(tmp_path):
    p = tmp_path / "corp.card"
    p.write_text(json.dumps({"card_id": "file", "version": "3"}))
    assert Glia.load(str(p)).card_id == "file"


@pytest.mark.parametrize("body,fragment", [
    ({"policies": [{"id": "a", "always": True}]}, "verdict"),
    ({"policies": [{"id": "a", "checkpoint": "on_input", "verdict": "deny",
                    "always": True}]}, "there are no checkpoints"),
    ({"policies": [{"id": "a", "verdict": "nope", "always": True}]},
     "unknown verdict"),
    ({"policies": [{"id": "a", "direction": "sideways", "verdict": "deny",
                    "always": True}]}, "unknown direction"),
    ({"policies": [{"id": "a", "verdict": "deny"}]}, "can never fire"),
    ({"policies": [
        {"id": "a", "verdict": "deny", "always": True},
        {"id": "a", "verdict": "deny", "always": True},
     ]}, "duplicate policy id"),
    ({"policies": [{"id": "a", "direction": "inbound", "types": ["RECALL"],
                    "verdict": "escalate", "always": True}]},
     "escalate is only offered"),
    ({"policies": [{"id": "a", "verdict": "escalate", "always": True}]},
     "escalate is only offered"),
    ({"policies": [{"id": "a", "verdict": "deny", "match": ["("]}]},
     "bad regex"),
    ({"policies": [{"id": "a", "types": ["TASK"], "verdict": "deny",
                    "always": True, "tools": ["x"]}]}, "tools= narrows"),
    ({"policies": [{"id": "a", "types": ["TASK"], "verdict": "deny",
                    "always": True, "ops": ["add"]}]}, "ops= narrows"),
    ({"policies": [{"id": "a", "types": ["TOOL_CALL"], "verdict": "deny",
                    "always": True, "retry": "resend"}]},
     "retry=resend repeats the request"),
    ({"policies": [{"id": "a", "verdict": "deny", "always": True,
                    "retry": "resend"}]}, "retry=resend repeats the request"),
    ({"policies": [{"id": "a", "types": ["IMPRINTED"], "verdict": "deny",
                    "always": True, "retry": "resend"}]},
     "retry=resend repeats the request"),
    ({"policies": [{"id": "a", "direction": "inbound", "types": ["TASK"],
                    "verdict": "deny", "always": True, "retry": "reask"}]},
     "no Neuron to re-ask"),
    ({"policies": [{"id": "a", "verdict": "redact", "match": ["x"],
                    "retry": "reask"}]}, "retry only applies to a deny"),
    ({"policies": [{"id": "a", "verdict": "deny", "always": True,
                    "retry": "sometimes"}]}, "unknown retry"),
    ({"policies": [{"id": "a", "verdict": "deny", "always": True,
                    "max_attempts": -1}]}, "max_attempts must be >= 0"),
    ({"nonsense": 1}, "unknown field"),
])
def test_a_malformed_card_is_refused_at_load_with_a_reason(body, fragment):
    with pytest.raises(PolicyError, match=fragment):
        Glia.load(json.dumps({"card_id": "x", **body}))


def test_a_decorator_is_refused_at_registration_for_the_same_reasons():
    c = Glia(card_id="d", mode="enforce")
    with pytest.raises(PolicyError, match="retry=resend"):
        c.on_signal(types=["TOOL_CALL"], retry="resend")(lambda sig: None)


def test_a_rule_narrows_by_tool_op_and_component():
    a = PolicyArtifact.parse(json.dumps({"policies": [
        {"id": "r", "types": ["TOOL_CALL"], "verdict": "deny",
         "always": True, "tools": ["shell"], "components": ["fx1"]},
        {"id": "w", "types": ["IMPRINT"], "verdict": "deny",
         "always": True, "ops": ["delete"]},
    ]}))
    tc = SignalType.TOOL_CALL
    assert a.evaluate(IN, tc, {"tool": "read"}, component="fx1") is None
    assert a.evaluate(IN, tc, {"tool": "shell"}, component="other") is None
    assert a.evaluate(IN, tc, {"tool": "shell"}, component="fx1") is not None
    im = SignalType.IMPRINT
    assert a.evaluate(IN, im, {"op": "add"}) is None
    assert a.evaluate(IN, im, {"op": "delete"}) is not None


async def test_the_artifact_is_evaluated_before_the_decorators():
    c = card(policies=[
        {"id": "artifact-wins", "verdict": "deny", "always": True,
         "reason": "from the artifact"},
    ])

    @c.on_signal
    def h(sig):  # pragma: no cover - the artifact answered first
        raise AssertionError("decorators run only when the artifact is silent")

    assert (await c.check(a_task(), direction=IN)).reason == "from the artifact"


# ---------------------------------------------------------------------------
# escalate and retry have to have somewhere to go
# ---------------------------------------------------------------------------


async def test_a_handler_escalating_where_there_is_no_channel_fails_closed():
    c = Glia(card_id="e", mode="enforce")

    @c.on_signal
    def h(sig):
        return Verdict.escalate("ask a human")

    oc = await c.check(an_imprint(), direction=IN)
    assert oc.denied, "an Engram has no escalation channel, so fail closed"
    oc = await c.check(a_task(), direction=IN)
    assert oc.escalated, "an Axon can escalate a TASK it was given"


def test_a_retry_with_nowhere_to_go_degrades_to_none():
    tr = SignalType.TOOL_RESULT
    assert allowed_retry(Retry.REASK, IN, tr, can_reask=False) is Retry.NONE
    assert allowed_retry(Retry.REASK, IN, tr, can_reask=True) is Retry.REASK
    assert allowed_retry(
        Retry.REASK, IN, SignalType.TASK, can_reask=True,
    ) is Retry.NONE
    assert allowed_retry(
        Retry.RESEND, OUT, SignalType.TOOL_CALL, can_reask=True,
    ) is Retry.NONE
    assert allowed_retry(Retry.RESEND, OUT, tr, can_reask=False) is Retry.RESEND


# ---------------------------------------------------------------------------
# There is no card lifetime
# ---------------------------------------------------------------------------


async def test_not_after_is_in_the_format_and_never_enforced():
    c = card(not_after="2000-01-01T00:00:00Z", policies=[
        {"id": "r", "verdict": "deny", "always": True},
    ])
    assert c.not_after is not None
    # Long expired, and still working: nothing phones home and nothing
    # fails closed because a management plane was unreachable.
    assert (await c.check(a_task(), direction=IN)).denied
    assert c.describe()["not_after"].startswith("2000-01-01")


def test_describe_lists_handlers_with_their_scope():
    c = Glia(card_id="d", mode="enforce")

    @c.on_signal(direction="inbound", types=["RECALLED"], retry="resend")
    def fresh(sig):
        return None

    [h] = c.describe()["handlers"]
    assert h == {"name": "fresh", "direction": "inbound",
                 "types": ["RECALLED"], "retry": "resend", "max_attempts": 2}


# ---------------------------------------------------------------------------
# Trace limits
# ---------------------------------------------------------------------------


def test_no_limit_mode_is_called_exact():
    assert "exact" not in TraceLimits.MODES


def test_a_limit_of_two_allows_exactly_two():
    counter = TraceCounter(TraceLimits(max_actions=2))
    for _ in range(2):
        assert counter.over("trc_1") is None
        counter.count("trc_1")
    assert counter.over("trc_1")[:1] == ("max_actions",)


def test_kind_none_counts_as_nothing():
    counter = TraceCounter(TraceLimits(max_actions=1))
    counter.count("trc_1", "none")
    assert counter.over("trc_1") is None


def test_mark_refused_latches_once():
    counter = TraceCounter(TraceLimits(max_actions=1))
    assert counter.mark_refused("trc_1") is True
    assert counter.mark_refused("trc_1") is False


def test_repair_policy_rejects_nonsense():
    with pytest.raises(PolicyError):
        RepairPolicy(max_attempts=-1)
