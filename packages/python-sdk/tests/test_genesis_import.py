"""
Opening projects Genesis didn't create.

`cosmo genesis` can import an existing project, which means deciding whether
an arbitrary folder is a Cosmonapse project at all, reading a layout nobody
guaranteed, and being honest about the parts it can't drive. The interesting
cases here are the ones that bite in practice:

  * folders that *use* cosmonapse but aren't projects - a scratch directory of
    scripts, or the SDK's own source tree - must be refused, not opened into
    an empty canvas;
  * a folder full of projects should offer them rather than dead-end;
  * component packages nest (the agent examples keep Neurons in
    ``neurons/model/``), and reading only the package's top level silently
    reported those projects as having none;
  * a component built by a factory has a config form but no behaviours,
    because there's no module-level object to decorate.

Where the real example projects are available they're used as the corpus -
they're the best evidence of what "a Cosmonapse project" actually looks like.
"""

import py_compile
import textwrap
from pathlib import Path

import pytest

from cosmo.commands import _genesis as G
from cosmo.commands import _genesis_ast as ga
from cosmo.commands.init import scaffold_project

# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------

def test_a_scaffolded_project_opens(tmp_path):
    target, _ = scaffold_project(str(tmp_path / "my-app"), namespace="demo")
    verdict = G._detect_project(str(target))

    assert verdict["is_project"]
    assert verdict["scaffolded"]
    assert verdict["reason"] is None
    # cosmo init scaffolds receptors/terminal.py too, and Genesis reads it.
    assert verdict["counts"] == {
        "neurons": 1, "engrams": 0, "effectors": 1, "receptors": 1,
    }
    assert "brain.py" in verdict["markers"]
    assert verdict["warnings"] == []


def test_a_folder_that_merely_imports_cosmonapse_is_refused(tmp_path):
    """The trap: a scratch folder of scripts, or the SDK's own tree."""
    (tmp_path / "scratch.py").write_text(
        "from cosmonapse import Axon\nprint(Axon)\n", encoding="utf-8",
    )
    verdict = G._detect_project(str(tmp_path))

    assert verdict["is_project"] is False
    assert "no brain.py" in verdict["reason"]


@pytest.mark.parametrize("contents,expected", [
    ({}, "No Python files here."),
    ({"notes.py": "print('hi')\n"}, "doesn't look like a Cosmonapse project"),
])
def test_refusals_say_why(tmp_path, contents, expected):
    for name, body in contents.items():
        (tmp_path / name).write_text(body, encoding="utf-8")
    verdict = G._detect_project(str(tmp_path))
    assert verdict["is_project"] is False
    assert expected in verdict["reason"]


def test_a_missing_path_is_refused_without_crashing(tmp_path):
    verdict = G._detect_project(str(tmp_path / "nope"))
    assert verdict["is_project"] is False
    assert verdict["children"] == []


def test_a_folder_of_projects_offers_them(tmp_path):
    for name in ("alpha", "beta"):
        scaffold_project(str(tmp_path / name), namespace="demo")
    (tmp_path / "not-a-project").mkdir()

    verdict = G._detect_project(str(tmp_path))
    assert verdict["is_project"] is False
    assert [c["name"] for c in verdict["children"]] == ["alpha", "beta"]
    assert verdict["children"][0]["counts"]["neurons"] == 1


def test_unreadable_subfolders_do_not_break_the_scan(tmp_path):
    scaffold_project(str(tmp_path / "alpha"), namespace="demo")
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o000)
    try:
        verdict = G._detect_project(str(tmp_path))
        assert [c["name"] for c in verdict["children"]] == ["alpha"]
    finally:
        locked.chmod(0o755)


# --------------------------------------------------------------------------
# Layouts Genesis didn't create
# --------------------------------------------------------------------------

def test_nested_component_packages_are_read(tmp_path):
    """The agent examples keep their Neurons in neurons/model/."""
    target, _ = scaffold_project(str(tmp_path / "my-app"), namespace="demo")
    nested = target / "neurons" / "model"
    nested.mkdir()
    (nested / "__init__.py").write_text("", encoding="utf-8")
    (nested / "planner.py").write_text(
        'from cosmonapse import Axon\n'
        'AXON = Axon(neuron_id="planner", neuron_fn=fn)\n',
        encoding="utf-8",
    )

    scaffold = G._read_scaffold(str(target))
    assert {n["id"] for n in scaffold["neurons"]} == {"hello", "planner"}
    # Addressed package-relative, posix style, so the Code tab can open it.
    assert "neurons/model/planner.py" in scaffold["files"]


def test_components_outside_the_standard_folders_are_reported(tmp_path):
    target, _ = scaffold_project(str(tmp_path / "my-app"), namespace="demo")
    (target / "smoke_test.py").write_text(
        'from cosmonapse import Effector\n'
        'FAKE = Effector.serve(effector_id="fake", effector_kind="test")\n',
        encoding="utf-8",
    )
    verdict = G._detect_project(str(target))
    warning = next(w for w in verdict["warnings"] if w["id"] == "stray-components")
    assert "smoke_test.py" in warning["text"]


def test_a_project_without_brain_py_opens_with_a_warning(tmp_path):
    target, _ = scaffold_project(str(tmp_path / "my-app"), namespace="demo")
    (target / "brain.py").unlink()

    verdict = G._detect_project(str(target))
    assert verdict["is_project"] is True      # component packages are enough
    assert verdict["scaffolded"] is False
    assert any(w["id"] == "no-brain" for w in verdict["warnings"])


# --------------------------------------------------------------------------
# Factory-built components
# --------------------------------------------------------------------------

FACTORY_MODULE = textwrap.dedent('''\
    """pool - N identical Neurons; the id comes from the caller."""
    from cosmonapse import Axon, Neuron


    def make_axon(neuron_id: str) -> Axon:
        return Axon(
            neuron_id=neuron_id,
            neuron_fn=Neuron(source="ollama", model="llama3"),
            capabilities=["chat"],
        )
    ''')


def test_a_factory_built_component_still_has_a_config_form():
    model = ga.parse_component(FACTORY_MODULE)
    decl = model["declaration"]

    assert model["kind"] == "neuron"
    assert decl["scope"] == "factory"
    assert decl["factory"] == "make_axon"
    by_name = {f["name"]: f for f in decl["fields"]}
    # The id is the function's parameter, not a literal - a reference.
    assert by_name["neuron_id"]["type"] == "name"
    assert by_name["neuron_fn"]["type"] == "expr"
    assert by_name["capabilities"]["value"] == ["chat"]


def test_editing_a_factory_keeps_it_inside_the_def(tmp_path):
    fields = ga.parse_component(FACTORY_MODULE)["declaration"]["fields"]
    fields[2] = {**fields[2], "value": ["chat", "summarise"]}
    out = ga.edit_declaration(FACTORY_MODULE, fields)

    assert '        capabilities=["chat", "summarise"],' in out
    assert "    return Axon(" in out
    # The nested Neuron(...) expression survives untouched.
    assert 'Neuron(source="ollama", model="llama3")' in out
    path = tmp_path / "pool.py"
    path.write_text(out, encoding="utf-8")
    py_compile.compile(str(path), doraise=True)


def test_behaviours_are_refused_on_a_factory_with_a_reason():
    with pytest.raises(ga.EditError, match="no\n?\\s*module-level object|module-level object"):
        ga.upsert_behavior(FACTORY_MODULE, behavior_id=None, scope="own",
                           protocol="before_task", fn_name="validate",
                           signature="input", body="return input")


def test_a_module_level_declaration_wins_over_a_factory():
    src = FACTORY_MODULE + '\nAXON = Axon(neuron_id="pool", neuron_fn=fn)\n'
    decl = ga.parse_component(src)["declaration"]
    assert decl["scope"] == "module"
    assert decl["target"] == "AXON"


# --------------------------------------------------------------------------
# The real corpus
# --------------------------------------------------------------------------
#
# cosmonapse-examples is the best available evidence of what real projects
# look like - 18 of them, written without Genesis in mind. Skipped when the
# checkout isn't next to this one.

EXAMPLES = Path(__file__).resolve().parents[3].parent / "cosmonapse-examples"

pytestmark_examples = pytest.mark.skipif(
    not EXAMPLES.is_dir(), reason="cosmonapse-examples checkout not available",
)


def _example_dirs():
    if not EXAMPLES.is_dir():
        return []
    return sorted(
        d for d in EXAMPLES.iterdir()
        if d.is_dir() and not d.name.startswith(".") and (d / "brain.py").is_file()
    )


@pytestmark_examples
@pytest.mark.parametrize("project", _example_dirs(), ids=lambda p: p.name)
def test_every_example_project_opens(project):
    verdict = G._detect_project(str(project))
    assert verdict["is_project"], verdict["reason"]
    assert sum(verdict["counts"].values()) > 0, "opened but shows no components"


@pytestmark_examples
@pytest.mark.parametrize("project", _example_dirs(), ids=lambda p: p.name)
def test_every_example_component_is_configurable_or_explained(project):
    """No component module may be a dead end in the Code tab.

    The earlier version of this test accepted ``declaration is None`` as an
    acceptable fallback, which is exactly how 22 of 42 components came to show
    nothing at all: ``EFFECTOR = MCPEffector(...)`` wasn't recognised because
    the constructor wasn't one of the SDK's. So the assertion is now the thing
    the user actually cares about - either the file yields a form, or it says
    specifically why it can't.
    """
    scaffold = G._read_scaffold(str(project))
    modules = [
        f"{pkg}/{node['file']}"
        for pkg, key in (("neurons", "neurons"), ("effector", "effectors"),
                         ("engram", "engrams"))
        for node in scaffold[key]
    ]
    assert modules, f"{project.name} reported no component modules"

    for rel in modules:
        model = G._component_model(str(project), rel)
        decl = model["declaration"]
        if decl is None:
            # Only acceptable when the module defines a component *class* -
            # there is genuinely nothing to configure, and the editor says so.
            assert model["defines"], (
                f"{project.name}/{rel}: no declaration and no class definition "
                "to explain it - this file would render as a dead end"
            )
            continue
        assert decl["scope"] in ("module", "factory")
        assert decl["target"] in ("AXON", "EFFECTOR", "ENGRAM") or decl["scope"] == "factory"
        assert model["kind"] in ("neuron", "effector", "engram")
        assert model["catalogue"] is not None
        # Every component can host Dendrite signal handlers, whatever it is.
        assert sum(len(g["protocols"]) for g in model["catalogue"]["host"]) > 20


@pytestmark_examples
def test_the_corpus_is_mostly_configurable():
    """A regression bound on the whole corpus, not just per project."""
    configurable = dead_ends = 0
    for project in _example_dirs():
        scaffold = G._read_scaffold(str(project))
        for pkg, key in (("neurons", "neurons"), ("effector", "effectors"),
                         ("engram", "engrams")):
            for node in scaffold[key]:
                model = G._component_model(str(project), f"{pkg}/{node['file']}")
                if model["declaration"] is not None:
                    configurable += 1
                elif not model["defines"]:
                    dead_ends += 1
    assert dead_ends == 0
    assert configurable >= 55, f"only {configurable} components yield a config form"


@pytestmark_examples
def test_the_examples_folder_itself_is_refused_but_offers_its_projects():
    verdict = G._detect_project(str(EXAMPLES))
    assert verdict["is_project"] is False
    names = {c["name"] for c in verdict["children"]}
    assert {"01-quickstart", "14-agent"} <= names
