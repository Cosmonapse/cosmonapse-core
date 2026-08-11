"""
Genesis's structured-editing layer.

`cosmo.commands._genesis_ast` is what lets the Code tab show a component as a
config form plus one code box per decorator, and write edits back. Its whole
contract is "change exactly the thing being edited and nothing else", so
that's what this file pins down:

  * every module the scaffold and `cosmo genesis` generate parses into the
    shape the UI expects;
  * an edit rewrites one span and leaves hand-written code byte-identical;
  * a rewrite that would break the module is refused rather than written;
  * an Engram can move between its three shapes, and the moves that would
    orphan behaviour are refused with a reason.

The HTTP tests at the bottom run the same operations through the real
aiohttp app, since the endpoints are how the UI actually reaches all of this.
"""

import ast
import textwrap

import pytest

from cosmo.commands import _genesis as G  # noqa: N812 - `g` is used pervasively below as a loop var
from cosmo.commands import _genesis_ast as ga
from cosmo.commands import _genesis_protocols as gp
from cosmo.commands.init import scaffold_project


@pytest.fixture
def project(tmp_path):
    """A scaffolded project with one of each primitive."""
    target, _ = scaffold_project(str(tmp_path / "my-app"), namespace="demo")
    for kind, name in (
        ("neuron", "summarize-notes"),
        ("effector", "http-tools"),
        ("engram", "notes"),
    ):
        G._create_component(str(target), kind, name)
    return target


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "rel,kind,shape,n_behaviors",
    [
        ("neurons/hello.py", "neuron", "axon", 0),
        ("neurons/summarize_notes.py", "neuron", "axon", 0),
        ("effector/tools.py", "effector", "served", 1),
        ("engram/notes.py", "engram", "served-over-backend", 2),
    ],
)
def test_every_generated_module_parses(project, rel, kind, shape, n_behaviors):
    model = ga.parse_component((project / rel).read_text())
    assert model["kind"] == kind
    assert model["shape"] == shape
    assert len(model["behaviors"]) == n_behaviors
    assert model["declaration"] is not None


def test_declaration_field_types_drive_the_form(project):
    model = ga.parse_component((project / "neurons/hello.py").read_text())
    by_name = {f["name"]: f for f in model["declaration"]["fields"]}
    assert by_name["neuron_id"]["type"] == "string"
    # A bare identifier is a reference, not a string - a text input would
    # round-trip it into `neuron_fn="hello"` and break the module.
    assert by_name["neuron_fn"]["type"] == "name"
    assert by_name["capabilities"]["type"] == "string_list"
    assert "hello" in model["async_fns"]


def test_non_literal_values_stay_expressions():
    src = textwrap.dedent('''\
        from cosmonapse import Axon, EngramBinding

        AXON = Axon(
            neuron_id="x",
            neuron_fn=fn,
            engrams=[EngramBinding(name="notes", directed_id="notes")],
        )
        ''')
    field = next(f for f in ga.parse_component(src)["declaration"]["fields"]
                 if f["name"] == "engrams")
    assert field["type"] == "expr"
    assert field["value"] == '[EngramBinding(name="notes", directed_id="notes")]'


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------

def test_editing_the_declaration_touches_only_the_declaration(project):
    src = (project / "neurons/hello.py").read_text()
    fields = ga.parse_component(src)["declaration"]["fields"]
    fields[0] = {**fields[0], "value": "greeter"}
    out = ga.edit_declaration(src, fields)

    assert src.split("AXON =")[0] == out.split("AXON =")[0]
    assert "async def hello" in out
    reparsed = ga.parse_component(out)
    assert reparsed["declaration"]["fields"][0]["value"] == "greeter"
    assert reparsed["declaration"]["fields"][1]["type"] == "name"


def test_hand_written_code_survives_edits_byte_for_byte(project):
    tail = textwrap.dedent('''\
        # a comment someone wrote by hand
        _CACHE: dict[str, str] = {}


        def _normalise(url: str) -> str:
            """Not a protocol thing at all - just my own helper."""
            return url.strip().rstrip("/")''')
    src = (project / "effector/http_tools.py").read_text() + "\n\n" + tail + "\n"

    model = ga.parse_component(src)
    assert [c["label"] for c in model["other"]] == [
        "Module docstring", "Imports", "Module code", "def _normalise",
    ]

    out = ga.edit_declaration(src, model["declaration"]["fields"])
    out = ga.upsert_behavior(out, behavior_id=None, scope="host",
                             protocol="on_tool_result", fn_name="observe",
                             signature="sig", body="return None")
    assert tail in out
    assert out.count("_CACHE") == 1
    ast.parse(out)


def test_behaviour_round_trip(project):
    src = (project / "neurons/hello.py").read_text()
    out = ga.upsert_behavior(
        src, behavior_id=None, scope="host", protocol="on_agent_output",
        fn_name="chain", signature="sig", body="return None",
        args=[{"name": "neuron", "type": "string", "value": "planner"}],
    )
    assert '@AXON.host.on_agent_output(neuron="planner")' in out

    b = ga.parse_component(out)["behaviors"][0]
    assert b["scope"] == "host"
    assert b["args"]["neuron"]["value"] == "planner"
    # The body is dedented for the code box, and re-indented on the way back.
    assert b["body"] == "return None"
    assert b["dedented"]

    out = ga.delete_behavior(out, b["id"])
    assert "on_agent_output" not in out
    ast.parse(out)


def test_a_body_that_would_not_parse_is_refused(project):
    src = (project / "neurons/hello.py").read_text()
    with pytest.raises(ga.EditError, match="would break the file"):
        ga.upsert_behavior(src, behavior_id=None, scope="own",
                           protocol="detects_error", fn_name="broken",
                           signature="raw", body="return (((")


def test_duplicate_handler_names_are_refused(project):
    src = (project / "engram/notes.py").read_text()
    with pytest.raises(ga.EditError, match="already has a handler"):
        ga.upsert_behavior(src, behavior_id=None, scope="own",
                           protocol="serves", fn_name="recall",
                           signature="query", body="return True")


# --------------------------------------------------------------------------
# Engram shape
# --------------------------------------------------------------------------

def test_shape_change_that_would_orphan_behaviour_is_refused(project):
    src = (project / "engram/notes.py").read_text()
    # A prebuilt backend has no hooks to register on_recall/on_imprint on.
    with pytest.raises(ga.EditError, match="no hooks"):
        ga.set_engram_shape(src, shape="prebuilt", backend="in-memory")
    # And dropping the storage would leave the handlers calling a dead name.
    with pytest.raises(ga.EditError, match="_backend"):
        ga.set_engram_shape(src, shape="served")


def test_full_shape_cycle(project):
    src = (project / "engram/notes.py").read_text()
    for b in ga.parse_component(src)["behaviors"]:
        src = ga.delete_behavior(src, b["id"])

    prebuilt = ga.set_engram_shape(src, shape="prebuilt", backend="sqlite")
    model = ga.parse_component(prebuilt)
    assert model["shape"] == "prebuilt"
    assert model["backend"] is None
    assert model["declaration"]["callee"] == "SqliteEngram"
    assert _cosmonapse_import(prebuilt) == ["SqliteEngram"]
    assert gp.catalogue("engram", "prebuilt")["own"] == []

    served = ga.set_engram_shape(prebuilt, shape="served-over-backend",
                                 backend="in-memory")
    model = ga.parse_component(served)
    assert model["shape"] == "served-over-backend"
    assert {b["protocol"] for b in model["behaviors"]} == {"on_recall", "on_imprint"}
    assert "_backend.recall(query, **kw)" in served
    assert _cosmonapse_import(served) == ["Engram", "InMemoryEngram"]
    # The id survives every hop.
    assert model["declaration"]["fields"][0]["value"] == "notes"
    ast.parse(served)


def _cosmonapse_import(src: str) -> list[str]:
    line = next(ln for ln in src.splitlines() if ln.startswith("from cosmonapse import"))
    return [n.strip() for n in line.split("import", 1)[1].split(",")]


# --------------------------------------------------------------------------
# The protocol catalogue
# --------------------------------------------------------------------------

def test_host_catalogue_is_read_off_dendrite():
    names = {p["name"] for g in gp.host_protocols() for p in g["protocols"]}
    assert {"on_agent_output", "on_tool_call", "on_recalled"} <= names
    # on_discover / on_trace have a non-standard registration shape, and
    # on_error & friends are deprecated aliases that would warn on use.
    assert not names & {"on_discover", "on_trace", "on_error", "on_register"}


def test_filters_come_from_the_real_signature():
    by_name = {p["name"]: p for g in gp.host_protocols() for p in g["protocols"]}
    assert [a["name"] for a in by_name["on_agent_output"]["decorator_args"]] == [
        "neuron", "capability", "trace_id",
    ]
    # on_task_offer takes capability/trace_id but not neuron.
    assert [a["name"] for a in by_name["on_task_offer"]["decorator_args"]] == [
        "capability", "trace_id",
    ]


@pytest.mark.parametrize("kind,shape,expected", [
    ("neuron", "axon", {"before_task", "detects_output", "on_connect"}),
    ("effector", "served", {"on_tool_call", "on_schedule"}),
    ("engram", "served-over-backend", {"on_recall", "on_imprint", "serves"}),
    ("engram", "prebuilt", set()),
])
def test_own_protocols_follow_the_shape(kind, shape, expected):
    cat = gp.catalogue(kind, shape)
    names = {p["name"] for g in cat["own"] for p in g["protocols"]}
    assert expected <= names
    if not expected:
        assert cat["own_empty_reason"]


# --------------------------------------------------------------------------
# Components built from a project's own classes
# --------------------------------------------------------------------------
#
# The SDK explicitly invites subclassing - "subclass the Effector ABC instead
# of Effector.serve() when a tool family needs its own connect()/close()
# lifecycle" - so a whitelist of SDK constructors is the wrong way to find a
# declaration. The name a project assigns to is the reliable signal.

SUBCLASS_MODULE = textwrap.dedent('''\
    """clock - an MCP server as an Effector."""
    from cosmonapse import Neuron

    from effector._mcp import MCPEffector

    EFFECTOR = MCPEffector(
        effector_id="clock-effector", effector_kind="time",
        mcp=Neuron(source="mcp", server="time"),
    )
    ''')

BASE_CLASS_MODULE = textwrap.dedent('''\
    """KeywordEngram - a BM25-lite lexical Engram backend."""
    from cosmonapse.engram.base import Engram


    class KeywordEngram(Engram):
        def __init__(self, engram_id: str = "keyword") -> None:
            self.engram_id = engram_id
    ''')


def test_a_component_built_from_a_project_class_is_still_a_declaration():
    model = ga.parse_component(SUBCLASS_MODULE)
    decl = model["declaration"]

    assert decl is not None, "an unrecognised constructor must not hide the component"
    assert model["kind"] == "effector"      # inferred from the EFFECTOR target
    assert model["shape"] == "custom"
    assert decl["callee"] == "MCPEffector"
    by_name = {f["name"]: f for f in decl["fields"]}
    assert by_name["effector_id"]["value"] == "clock-effector"
    assert by_name["mcp"]["type"] == "expr"


def test_a_custom_component_offers_host_protocols_only():
    cat = gp.catalogue("effector", "custom", "MCPEffector")
    assert cat["own"] == []
    assert "can't tell which hooks it carries" in cat["own_empty_reason"]
    # Host decorators live on the base classes, so they always apply.
    assert sum(len(g["protocols"]) for g in cat["host"]) > 20


def test_editing_a_custom_component_keeps_its_constructor():
    fields = ga.parse_component(SUBCLASS_MODULE)["declaration"]["fields"]
    fields[1] = {**fields[1], "value": "clock"}
    out = ga.edit_declaration(SUBCLASS_MODULE, fields)

    assert "EFFECTOR = MCPEffector(" in out
    assert 'effector_kind="clock"' in out
    assert 'mcp=Neuron(source="mcp", server="time")' in out
    ast.parse(out)


def test_a_module_defining_a_backend_class_is_explained_not_failed():
    model = ga.parse_component(BASE_CLASS_MODULE)
    assert model["declaration"] is None      # nothing to configure - correct
    assert model["defines"] == [
        {"name": "KeywordEngram", "base": "Engram", "kind": "engram"},
    ]
    assert model["kind"] == "engram"


def test_shape_changes_are_refused_on_a_project_defined_engram():
    src = 'from myproj import CustomEngram\nENGRAM = CustomEngram(engram_id="x")\n'
    with pytest.raises(ga.EditError, match="own Engram class"):
        ga.set_engram_shape(src, shape="prebuilt", backend="in-memory")


# --------------------------------------------------------------------------
# Every shape a declaration turns up in
# --------------------------------------------------------------------------
#
# A user reported "I don't see the Axon declaration", and the cause was that
# detection keyed off a whitelist of SDK constructors. These are the forms a
# declaration actually takes in the wild; each one that goes unrecognised
# renders as a blank editor, which is the worst possible failure mode - it
# looks like the feature is broken rather than like the file is unusual.

@pytest.mark.parametrize("label,src,expect_shape,expect_target", [
    ("plain",
     'from cosmonapse import Axon\nAXON = Axon(neuron_id="a", neuron_fn=f)\n',
     "axon", "AXON"),
    ("classmethod",
     'from cosmonapse import Axon\nAXON = Axon.ollama("a", model="llama3")\n',
     "axon", "AXON"),
    ("project subclass",
     'from mine import MyAxon\nAXON = MyAxon(neuron_id="a", neuron_fn=f)\n',
     "custom", "AXON"),
    ("annotated",
     'from cosmonapse import Axon\nAXON: Axon = Axon(neuron_id="a", neuron_fn=f)\n',
     "axon", "AXON"),
    ("factory returning the constructor",
     'from cosmonapse import Axon\ndef make(n):\n    return Axon(neuron_id=n, neuron_fn=f)\n',
     "axon", "make"),
    ("factory assigning then returning",
     'from cosmonapse import Axon\ndef make(n):\n    a = Axon(neuron_id=n, neuron_fn=f)\n    return a\n',
     "axon", "make"),
    ("unconventional name",
     'from cosmonapse import Axon\naxon = Axon(neuron_id="a", neuron_fn=f)\n',
     "axon", "axon"),
])
def test_a_declaration_is_found_however_it_is_written(label, src, expect_shape, expect_target):
    model = ga.parse_component(src)
    decl = model["declaration"]
    assert decl is not None, f"{label}: would render as an empty editor"
    assert model["kind"] == "neuron"
    assert model["shape"] == expect_shape
    assert decl["target"] == expect_target
    # Axon.ollama takes the id positionally, so it isn't a keyword field -
    # but it must survive a save (see the round-trip test below).
    assert decl["fields"] or decl["verbatim"][0]


def test_editing_a_factory_that_assigns_before_returning():
    src = ('from cosmonapse import Axon\n'
           'def make(n):\n'
           '    a = Axon(\n        neuron_id=n,\n        capabilities=["chat"],\n    )\n'
           '    return a\n')
    fields = ga.parse_component(src)["declaration"]["fields"]
    fields[1] = {**fields[1], "value": ["chat", "summarise"]}
    out = ga.edit_declaration(src, fields)

    assert "    a = Axon(" in out          # the binding is preserved, not turned into a return
    assert "    return a" in out
    assert '        capabilities=["chat", "summarise"],' in out
    ast.parse(out)


def test_a_conventional_target_wins_over_a_loose_one():
    src = ('from cosmonapse import Axon\n'
           'spare = Axon(neuron_id="spare", neuron_fn=f)\n'
           'AXON = Axon(neuron_id="real", neuron_fn=f)\n')
    decl = ga.parse_component(src)["declaration"]
    assert decl["target"] == "AXON"
    assert decl["fields"][0]["value"] == "real"


@pytest.mark.parametrize("src", [
    'from cosmonapse import Axon\nAXON = Axon.ollama("my-neuron", model="llama3")\n',
    'from cosmonapse import Axon\nAXON = Axon(neuron_id="a", neuron_fn=f, **overrides)\n',
    'from cosmonapse import Axon\nAXON = Axon.openai("a", *extra, model="gpt", **kw)\n',
    'from cosmonapse import Effector\nEFFECTOR = Effector.serve(effector_id="e")\n',
])
def test_saving_a_form_never_drops_an_argument(src):
    """The regression that matters most: a save must not delete code.

    Rendering the call from its keyword arguments alone silently deleted
    positional and ``**kwargs`` arguments - so editing anything about an
    ``Axon.ollama("my-neuron", ...)`` removed the neuron_id and left a module
    that wouldn't import. A form that can't edit an argument is a limitation;
    a form that deletes one is a bug.
    """
    before = ga.parse_component(src)["declaration"]
    # A no-op save: hand back exactly the fields that were parsed out.
    out = ga.edit_declaration(src, before["fields"])
    after = ga.parse_component(out)["declaration"]

    assert after["verbatim"] == before["verbatim"], "an unmodelled argument was lost"
    assert {f["name"] for f in after["fields"]} == {f["name"] for f in before["fields"]}
    assert after["callee"] == before["callee"]
    ast.parse(out)


def test_positional_arguments_survive_a_real_edit():
    src = 'from cosmonapse import Axon\nAXON = Axon.ollama("my-neuron", model="llama3")\n'
    fields = ga.parse_component(src)["declaration"]["fields"]
    fields[0] = {**fields[0], "value": "llama3.1"}
    out = ga.edit_declaration(src, fields)

    assert '"my-neuron",' in out
    assert 'model="llama3.1",' in out
    ast.parse(out)


# --------------------------------------------------------------------------
# Axon source
# --------------------------------------------------------------------------
# The Neuron-side analogue of the Engram shape moves above. An Axon wraps
# either a function the project wrote or a provider the SDK builds, and the
# three ways of writing that do NOT take the same keywords - which is the
# whole reason this is a structural operation and not a form field.

_HEAD = "from cosmonapse import Axon\n\n\n"


@pytest.mark.parametrize("label,src,source,form", [
    ("explicit",
     ('async def f(input, context) -> dict:\n    return {}\n\n\n'
      'AXON = Axon(neuron_id="n", neuron_fn=f)\n'), "custom", "explicit"),
    ("paired", 'AXON = Axon.ollama(neuron_id="n", model="llama3")\n',
     "ollama", "paired"),
    ("paired alias", 'AXON = Axon.hf(neuron_id="n", endpoint="http://x")\n',
     "huggingface", "paired"),
    ("from_source positional",
     'AXON = Axon.from_source("groq", neuron_id="n", model="m")\n',
     "groq", "from_source"),
    # source is positional-OR-keyword, so a form that only read args would
    # report no provider at all for this one.
    ("from_source keyword",
     'AXON = Axon.from_source(source="mistral", neuron_id="n")\n',
     "mistral", "from_source"),
])
def test_an_axons_provider_and_form_are_read_off_the_call(label, src, source, form):
    decl = ga.parse_component(_HEAD + src)["declaration"]
    assert decl["source"] == source, label
    assert decl["form"] == form, label


def test_switching_provider_drops_the_old_providers_keywords():
    src = _HEAD + ('AXON = Axon.openai(\n    neuron_id="n",\n    model="gpt-4o",\n'
                   '    capabilities=["chat"],\n    max_tokens=100,\n)\n')
    out = ga.set_axon_source(src, source="ollama")
    decl = ga.parse_component(out)["declaration"]

    assert decl["callee"] == "Axon.ollama"
    names = {f["name"]: f["value"] for f in decl["fields"]}
    # Identity and wiring are the Axon's, so they cross.
    assert names["neuron_id"] == "n"
    assert names["capabilities"] == ["chat"]
    # max_tokens is a legal Ollama keyword too, but its VALUE was an answer
    # about a different model - carrying it by name is the bug this guards.
    assert "max_tokens" not in names
    assert "model" not in names


def test_switching_form_keeps_the_provider_configuration():
    src = _HEAD + 'AXON = Axon.ollama(neuron_id="n", model="llama3", temperature=0.2)\n'
    out = ga.set_axon_source(src, source="ollama", form="from_source")
    decl = ga.parse_component(out)["declaration"]

    assert decl["callee"] == "Axon.from_source"
    assert decl["source"] == "ollama"
    names = {f["name"]: f["value"] for f in decl["fields"]}
    assert names["model"] == "llama3"
    assert names["temperature"] == 0.2
    # ...and back, unchanged.
    assert ga.parse_component(
        ga.set_axon_source(out, source="ollama", form="paired"),
    )["declaration"]["callee"] == "Axon.ollama"


def test_the_explicit_form_recovers_a_neuron_fn_from_the_module():
    src = _HEAD + ('async def greet(input, context) -> dict:\n    return {}\n\n\n'
                   'AXON = Axon.ollama(neuron_id="n", model="llama3")\n')
    decl = ga.parse_component(ga.set_axon_source(src, source="custom"))["declaration"]
    assert decl["callee"] == "Axon"
    assert {"name": "neuron_fn", "type": "name", "value": "greet"} in decl["fields"]


def test_a_positional_neuron_id_survives_the_switch():
    # Axon.ollama takes neuron_id positionally; Axon.from_source's only
    # positional slot is the source, so leaving it there would rename it.
    out = ga.set_axon_source(_HEAD + 'AXON = Axon.ollama("n", model="llama3")\n',
                             source="together")
    decl = ga.parse_component(out)["declaration"]
    assert decl["callee"] == "Axon.from_source"
    assert decl["fields"][0] == {"name": "neuron_id", "type": "string", "value": "n"}


def test_switching_source_never_needs_a_new_import():
    # Unlike an Engram shape change, every form is still `Axon`, so the
    # import line is not this operation's business.
    src = _HEAD + 'AXON = Axon.ollama(neuron_id="n", model="llama3")\n'
    for source in ("custom", "openai", "groq", "mcp"):
        try:
            out = ga.set_axon_source(src, source=source)
        except ga.EditError:
            continue          # custom is refused here - no async fn to wrap
        assert _cosmonapse_import(out) == ["Axon"]


@pytest.mark.parametrize("src,kwargs,match", [
    # A provider with no classmethod of its own.
    (f'{_HEAD}AXON = Axon.ollama(neuron_id="n")\n',
     dict(source="groq", form="paired"), "no Axon.groq"),
    (f'{_HEAD}AXON = Axon.ollama(neuron_id="n")\n',
     dict(source="gemini"), "unknown neuron source"),
    # The explicit form is what "custom" means; the pair can't be split.
    (f'{_HEAD}AXON = Axon.ollama(neuron_id="n")\n',
     dict(source="ollama", form="explicit"), "hand-written Neuron is the explicit form"),
    # Nothing in the module to point neuron_fn at.
    (f'{_HEAD}AXON = Axon.ollama(neuron_id="n")\n',
     dict(source="custom"), "needs an async function"),
    # Contents of **kwargs can't be attributed to either provider.
    (f'{_HEAD}AXON = Axon.ollama(**cfg)\n', dict(source="openai"), r"\*\*kwargs"),
    # A project's own subclass isn't ours to replace.
    ('from myapp import MyAxon\n\nAXON = MyAxon(neuron_id="n")\n',
     dict(source="openai"), "own Axon subclass"),
    ('from cosmonapse import Engram\n\nENGRAM = Engram.serve(engram_id="e")\n',
     dict(source="openai"), "doesn't declare an Axon"),
])
def test_an_unsafe_source_change_is_refused(src, kwargs, match):
    with pytest.raises(ga.EditError, match=match):
        ga.set_axon_source(src, **kwargs)


def test_switching_to_what_it_already_is_is_a_no_op():
    src = _HEAD + 'AXON = Axon.ollama(neuron_id="n", model="llama3")\n'
    assert ga.set_axon_source(src, source="ollama") == src


def test_a_factory_built_axon_can_be_repointed(project):
    src = _HEAD + (
        'def make_axon(neuron_id: str) -> Axon:\n'
        '    return Axon.ollama(neuron_id=neuron_id, model="llama3")\n'
    )
    out = ga.set_axon_source(src, source="anthropic")
    decl = ga.parse_component(out)["declaration"]
    assert decl["scope"] == "factory"
    assert decl["callee"] == "Axon.anthropic"
    # The constructor stayed inside its def.
    assert "    return Axon.anthropic(" in out
    ast.parse(out)


# --------------------------------------------------------------------------
# The config form each form gets
# --------------------------------------------------------------------------

def test_a_paired_axon_is_not_asked_for_a_neuron_fn():
    # Axon.from_source builds the Neuron, so neuron_fn and output_parser are
    # TypeErrors there - offering them was the original defect.
    explicit = {f["name"] for f in gp.declaration_fields("Axon")}
    paired = {f["name"] for f in gp.declaration_fields("Axon.ollama")}
    assert {"neuron_fn", "output_parser"} <= explicit
    assert not ({"neuron_fn", "output_parser"} & paired)
    # ...and the provider's own keywords are offered instead.
    assert {"model", "endpoint", "system", "recognize", "teach_intents"} <= paired


def test_from_source_takes_its_provider_from_the_declaration():
    groq = {f["name"] for f in gp.declaration_fields("Axon.from_source", "groq")}
    anthropic = {f["name"] for f in gp.declaration_fields("Axon.from_source", "anthropic")}
    assert "max_new_tokens" in groq and "max_new_tokens" not in anthropic
    assert "max_tokens" in anthropic


def test_mcp_is_never_offered_intent_teaching():
    # Its wrapper takes no system=, so teach_intents=True raises.
    mcp = {f["name"] for f in gp.declaration_fields("Axon.mcp")}
    assert "recognize" in mcp
    assert "teach_intents" not in mcp


def test_tool_standard_suggests_dialects_not_providers():
    # The original table suggested openai/anthropic/mcp - model providers,
    # every one of which raises ValueError as a tool_standard.
    from cosmonapse.effector.standards import TOOL_STANDARDS

    field = next(f for f in gp.declaration_fields("Axon") if f["name"] == "tool_standard")
    assert set(field["suggest"]) == set(TOOL_STANDARDS)


# --------------------------------------------------------------------------
# Neuron prompt
# --------------------------------------------------------------------------
#
# The prompt is a module constant rather than a keyword, which is exactly why
# it needed its own section: it is the most-edited text in an LLM Neuron and
# the config form has never been able to reach it. What these pin down is
# that lifting it into an editable box didn't cost the file anything - an
# unchanged save is byte-identical, and the two shapes Genesis can't rewrite
# safely are refused instead of flattened.

_NEURON = '''\
"""A stock LLM neuron."""
import os

from cosmonapse import Axon

#: which model to talk to
MODEL = os.environ.get("M", "llama3")

SYSTEM = (
    "You are a research specialist. Using ONLY the supplied web context, "
    "write tight, factual notes."
)

AXON = Axon.ollama(
    neuron_id="research",
    model=MODEL,
)


@AXON.before_task
async def gather(input):
    return {"messages": [{"role": "system", "content": SYSTEM}]}
'''


def test_the_prompt_is_read_out_of_the_module():
    model = ga.parse_component(_NEURON)
    prompt = model["prompt"]
    assert prompt["name"] == "SYSTEM"
    assert prompt["text"].startswith("You are a research specialist.")
    assert prompt["editable"] is True
    assert prompt["used"] is True
    # And it leaves the read-only chunks, which is the point of the section.
    assert not any("You are a research specialist" in c["text"] for c in model["other"])


def test_a_prompt_the_module_never_reads_is_reported():
    src = _NEURON.replace('{"role": "system", "content": SYSTEM}', '{"role": "user"}')
    assert ga.parse_component(src)["prompt"]["used"] is False


def test_only_a_neuron_gets_a_prompt_section():
    src = _NEURON.replace("from cosmonapse import Axon", "from cosmonapse import Effector") \
                 .replace("AXON = Axon.ollama(\n    neuron_id=\"research\",\n    model=MODEL,\n)",
                          "EFFECTOR = Effector.serve(effector_id=\"tools\")") \
                 .replace("@AXON.before_task", "@EFFECTOR.on_tool_call")
    model = ga.parse_component(src)
    assert model["kind"] == "effector"
    assert model["prompt"] is None
    # Someone else's constant stays in the read-only chunks, untouched.
    assert any("You are a research specialist" in c["text"] for c in model["other"])


def test_saving_an_unchanged_prompt_leaves_the_file_alone():
    text = ga.parse_component(_NEURON)["prompt"]["text"]
    assert ga.set_prompt(_NEURON, prompt=text) == _NEURON


def test_editing_the_prompt_touches_only_the_prompt():
    out = ga.set_prompt(_NEURON, prompt="Be terse.")
    assert ga.parse_component(out)["prompt"]["text"] == "Be terse."
    assert 'SYSTEM = "Be terse."' in out
    # Everything else survives, including the declaration and the hook.
    for kept in ("#: which model to talk to", "MODEL = os.environ.get", "AXON = Axon.ollama(",
                 "async def gather(input):"):
        assert kept in out
    ast.parse(out)


@pytest.mark.parametrize("prompt", [
    "One line.",
    "Two\nlines.",
    ("A paragraph.\n\nAnother paragraph, with a much longer run of words in it "
     "so that the renderer has to break the literal somewhere sensible."),
    'JSON in it: {"route": "research", "task": "<what to find out>"}',
    "Both quote kinds: \"quoted\" and 'quoted', plus a tab\there.",
    "Trailing newline.\n",
    "   leading and trailing spaces   matter   ",
])
def test_a_prompt_round_trips_through_the_file(prompt):
    """Whatever goes in the box comes back out of the file unchanged.

    The renderer wraps long prompts across several literals, and a wrap that
    drops the space it broke on is a silent change to what the model is told.
    """
    out = ga.set_prompt(_NEURON, prompt=prompt)
    assert ga.parse_component(out)["prompt"]["text"] == prompt
    assert max(len(ln) for ln in out.splitlines()) <= 79


def test_a_json_heavy_prompt_is_not_written_as_escaped_quotes():
    out = ga.set_prompt(_NEURON, prompt='Reply with {"route": "research"} and nothing else.')
    assert '\\"' not in out


def test_a_prompt_is_added_to_a_module_that_has_none():
    src = _NEURON.replace(
        'SYSTEM = (\n'
        '    "You are a research specialist. Using ONLY the supplied web context, "\n'
        '    "write tight, factual notes."\n'
        ')\n\n', "")
    assert ga.parse_component(src)["prompt"] is None
    out = ga.set_prompt(src, prompt="Be terse.")
    model = ga.parse_component(out)
    assert model["prompt"]["text"] == "Be terse."
    # Written at the top of the body, under the imports - and above the
    # comment that belongs to the constant below it, not between them.
    assert out.index("SYSTEM") < out.index("#: which model")
    assert out.index("from cosmonapse import Axon") < out.index("SYSTEM")
    ast.parse(out)


def test_an_empty_prompt_is_refused():
    with pytest.raises(ga.EditError, match="needs some text"):
        ga.set_prompt(_NEURON, prompt="   ")


def test_a_module_with_no_axon_has_nothing_to_prompt():
    with pytest.raises(ga.EditError, match="doesn't declare an Axon"):
        ga.set_prompt("ENGRAM = Engram.serve(engram_id='notes')\n", prompt="hi")


def test_a_built_prompt_is_shown_but_not_edited():
    """15-claude-harness interpolates the date into its prompt.

    Flattening that to a quoted literal would freeze today's date into the
    file, so the card shows it and sends the user to their editor.
    """
    src = _NEURON.replace(
        '    "You are a research specialist. Using ONLY the supplied web context, "\n'
        '    "write tight, factual notes."\n',
        '    "You are a research specialist. "\n'
        '    f"Today is {datetime.date.today()}."\n')
    prompt = ga.parse_component(src)["prompt"]
    assert prompt["editable"] is False
    assert prompt["note"] == "built rather than written"
    assert prompt["source"].startswith("SYSTEM = (")
    with pytest.raises(ga.EditError, match="built rather than written"):
        ga.set_prompt(src, prompt="Be terse.")


def test_a_prompt_with_comments_inside_it_is_not_rewritten():
    """The comments between the pieces are the valuable half of that file.

    A save replaces the constant as a unit, so there is nowhere for them to
    go - refusing beats deleting an explanation the user can't see in the box.
    """
    src = _NEURON.replace(
        '    "You are a research specialist. Using ONLY the supplied web context, "\n',
        '    "You are a research specialist. "\n'
        '    # Language pin: the served alias drifts into Thai without it.\n'
        '    "Always reply in English. "\n')
    prompt = ga.parse_component(src)["prompt"]
    assert prompt["editable"] is False
    assert prompt["note"] == "written with comments between its pieces"
    with pytest.raises(ga.EditError, match="comments"):
        ga.set_prompt(src, prompt="Be terse.")


def test_a_hash_inside_the_prompt_is_not_a_comment():
    """Two of the examples use "# Tools" as a heading in the prompt itself."""
    out = ga.set_prompt(_NEURON, prompt="# Tools\n\nYou may call functions.")
    assert ga.parse_component(out)["prompt"]["editable"] is True


def test_the_conventional_names_are_all_read_back():
    for name in ga.PROMPT_NAMES:
        src = _NEURON.replace("SYSTEM = (", f"{name} = (").replace("content\": SYSTEM", f"content\": {name}")
        assert ga.parse_component(src)["prompt"]["name"] == name


def test_a_second_binding_of_the_name_is_refused():
    src = _NEURON.replace(
        'SYSTEM = (\n'
        '    "You are a research specialist. Using ONLY the supplied web context, "\n'
        '    "write tight, factual notes."\n'
        ')', 'def SYSTEM():\n    return "built elsewhere"')
    with pytest.raises(ga.EditError, match="already binds SYSTEM"):
        ga.set_prompt(src, prompt="Be terse.")
