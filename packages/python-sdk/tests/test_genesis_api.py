"""
The Genesis local API, exercised the way the UI reaches it.

`cosmo genesis` is a single-page app talking to an aiohttp server on
127.0.0.1, and by now most of Genesis *is* that API - scaffolding a project,
adding components, and every structured edit the Code tab makes. These tests
drive the real app object (see ``_genesis.build_app``) over HTTP rather than
calling the helpers directly, so the routing, the JSON envelopes and the
status codes are covered too.

Two invariants get the most attention, because they're the ones a user would
feel: an edit that wouldn't compile is refused *without touching the file*,
and no path outside the project can be read or written.
"""

import py_compile

import pytest
from aiohttp.test_utils import TestClient, TestServer

from cosmo.commands._genesis import build_app
from cosmo.commands.init import scaffold_project


@pytest.fixture
async def api(tmp_path):
    """A running Genesis API plus a scaffolded project for it to work on."""
    target, _ = scaffold_project(str(tmp_path / "my-app"), namespace="demo")
    client = TestClient(TestServer(build_app(None)))
    await client.start_server()

    class Api:
        path = str(target)
        root = target

        async def get(self, url, **params):
            r = await client.get(url, params=params)
            return r.status, await r.json()

        async def post(self, url, **body):
            r = await client.post(url, json=body)
            return r.status, await r.json()

    yield Api()
    await client.close()


async def _add(api, kind, name):
    status, body = await api.post("/api/component", path=api.path, kind=kind, name=name)
    assert status == 200, body
    return body


# --------------------------------------------------------------------------
# Components
# --------------------------------------------------------------------------

@pytest.mark.parametrize("kind,name,module", [
    ("neuron", "summarize-notes", "neurons/summarize_notes.py"),
    ("effector", "http-tools", "effector/http_tools.py"),
    ("engram", "notes", "engram/notes.py"),
])
async def test_adding_a_component_writes_the_module_and_wires_brain(api, kind, name, module):
    body = await _add(api, kind, name)
    assert body["wired"], body["note"]
    assert (api.root / module).is_file()
    py_compile.compile(str(api.root / module), doraise=True)
    py_compile.compile(str(api.root / "brain.py"), doraise=True)


async def test_bad_component_names_are_rejected(api):
    status, body = await api.post("/api/component", path=api.path,
                                  kind="neuron", name="Not Valid!")
    assert status == 400
    assert body["exists"] is False

    await _add(api, "neuron", "twice")
    status, body = await api.post("/api/component", path=api.path,
                                  kind="neuron", name="twice")
    assert status == 409
    assert body["exists"] is True


# --------------------------------------------------------------------------
# The interactive Code tab
# --------------------------------------------------------------------------

async def test_model_exposes_form_behaviours_and_catalogue(api):
    await _add(api, "engram", "notes")
    status, model = await api.get("/api/model", path=api.path, file="engram/notes.py")
    assert status == 200
    assert model["shape"] == "served-over-backend"
    assert model["backend"]["backend"] == "in-memory"
    assert {b["protocol"] for b in model["behaviors"]} == {"on_recall", "on_imprint"}

    own = {p["name"] for g in model["catalogue"]["own"] for p in g["protocols"]}
    assert {"on_recall", "on_imprint", "serves"} <= own
    assert sum(len(g["protocols"]) for g in model["catalogue"]["host"]) > 20


async def test_declaration_form_round_trips(api):
    await _add(api, "engram", "notes")
    _, model = await api.get("/api/model", path=api.path, file="engram/notes.py")

    fields = model["declaration"]["fields"]
    fields[1]["value"] = "context"
    fields.append({"name": "capabilities", "type": "string_list",
                   "value": ["substring", "tags"]})

    status, updated = await api.post("/api/declaration", path=api.path,
                                     file="engram/notes.py", fields=fields)
    assert status == 200
    saved = {f["name"]: f["value"] for f in updated["declaration"]["fields"]}
    assert saved["engram_kind"] == "context"
    assert saved["capabilities"] == ["substring", "tags"]
    assert 'engram_kind="context"' in (api.root / "engram/notes.py").read_text()


async def test_behaviour_add_edit_delete(api):
    await _add(api, "engram", "notes")
    status, model = await api.post(
        "/api/behavior", path=api.path, file="engram/notes.py",
        scope="host", protocol="on_imprint_signal", fn_name="audit",
        signature="sig", body="return None",
        args=[{"name": "trace_id", "type": "string", "value": "abc"}],
    )
    assert status == 200
    added = next(b for b in model["behaviors"] if b["fn_name"] == "audit")
    assert added["args"]["trace_id"]["value"] == "abc"

    source = (api.root / "engram/notes.py").read_text()
    assert '@ENGRAM.host.on_imprint_signal(trace_id="abc")' in source
    # The handlers that were already there are untouched.
    assert "_backend.recall(query, **kw)" in source
    assert "_backend.imprint(op, entry, **kw)" in source

    status, model = await api.post("/api/behavior/delete", path=api.path,
                                   file="engram/notes.py", behavior_id=added["id"])
    assert status == 200
    assert "audit" not in {b["fn_name"] for b in model["behaviors"]}


async def test_an_edit_that_would_not_compile_leaves_the_file_alone(api):
    await _add(api, "engram", "notes")
    before = (api.root / "engram/notes.py").read_text()

    status, body = await api.post("/api/behavior", path=api.path,
                                  file="engram/notes.py", scope="own",
                                  protocol="serves", fn_name="gate",
                                  signature="query", body="return (((")
    assert status == 400
    assert "never closed" in body["error"]
    assert (api.root / "engram/notes.py").read_text() == before


async def test_engram_shape_cycle_over_http(api):
    await _add(api, "engram", "notes")
    _, model = await api.get("/api/model", path=api.path, file="engram/notes.py")

    status, body = await api.post("/api/engram-shape", path=api.path,
                                  file="engram/notes.py", shape="prebuilt",
                                  backend="in-memory")
    assert status == 400 and "no hooks" in body["error"]

    for behavior in model["behaviors"]:
        status, _ = await api.post("/api/behavior/delete", path=api.path,
                                   file="engram/notes.py",
                                   behavior_id=behavior["id"])
        assert status == 200

    status, prebuilt = await api.post("/api/engram-shape", path=api.path,
                                      file="engram/notes.py", shape="prebuilt",
                                      backend="sqlite")
    assert status == 200
    assert prebuilt["shape"] == "prebuilt"
    assert prebuilt["catalogue"]["own"] == []
    assert prebuilt["catalogue"]["own_empty_reason"]
    # The sqlite backend takes a `path`, so the form offers one.
    assert prebuilt["catalogue"]["declaration_fields"][0]["name"] == "path"

    status, served = await api.post("/api/engram-shape", path=api.path,
                                    file="engram/notes.py",
                                    shape="served-over-backend",
                                    backend="in-memory")
    assert status == 200
    assert {b["fn_name"] for b in served["behaviors"]} == {"recall", "imprint"}
    py_compile.compile(str(api.root / "engram/notes.py"), doraise=True)


async def test_axon_source_cycle_over_http(api):
    """The scaffold's Neuron, repointed at a provider and back."""
    await _add(api, "neuron", "summarize-notes")
    rel = "neurons/summarize_notes.py"

    _, model = await api.get("/api/model", path=api.path, file=rel)
    assert model["declaration"]["source"] == "custom"
    assert model["declaration"]["form"] == "explicit"

    status, paired = await api.post("/api/axon-source", path=api.path,
                                    file=rel, source="anthropic")
    assert status == 200
    assert paired["declaration"]["callee"] == "Axon.anthropic"
    assert paired["declaration"]["form"] == "paired"
    # The form follows the source: Anthropic's keywords, and no neuron_fn,
    # which Axon.from_source refuses to accept.
    offered = {f["name"] for f in paired["catalogue"]["declaration_fields"]}
    assert {"model", "api_key", "max_tokens", "teach_intents"} <= offered
    assert "neuron_fn" not in offered
    # Switching the provider is not switching what the node can host.
    assert paired["catalogue"]["own"] == model["catalogue"]["own"]

    status, sourced = await api.post("/api/axon-source", path=api.path,
                                     file=rel, source="groq")
    assert status == 200
    assert sourced["declaration"]["callee"] == "Axon.from_source"
    assert sourced["declaration"]["source"] == "groq"

    # The scaffold's async function is still there, so explicit is reachable.
    status, back = await api.post("/api/axon-source", path=api.path,
                                  file=rel, source="custom")
    assert status == 200
    assert back["declaration"]["callee"] == "Axon"
    assert any(f["name"] == "neuron_fn" for f in back["declaration"]["fields"])
    py_compile.compile(str(api.root / rel), doraise=True)


async def test_a_refused_source_change_leaves_the_file_alone(api):
    await _add(api, "neuron", "summarize-notes")
    rel = "neurons/summarize_notes.py"
    before = (api.root / rel).read_text()

    status, body = await api.post("/api/axon-source", path=api.path,
                                  file=rel, source="groq", form="paired")
    assert status == 400
    assert "no Axon.groq" in body["error"]
    assert (api.root / rel).read_text() == before


# --------------------------------------------------------------------------
# helpers.py
# --------------------------------------------------------------------------

async def test_helpers_is_created_once_and_must_stay_valid(api):
    status, body = await api.post("/api/helpers", path=api.path)
    assert status == 200 and body["created"] is True
    status, body = await api.post("/api/helpers", path=api.path)
    assert body["created"] is False

    status, _ = await api.post("/api/file", path=api.path, file="helpers.py",
                               text='def hi(n):\n    return f"hi {n}"\n')
    assert status == 200

    status, body = await api.post("/api/file", path=api.path,
                                  file="helpers.py", text="def broken(:\n")
    assert status == 400
    _, read = await api.get("/api/file", path=api.path, file="helpers.py")
    assert "def hi(n)" in read["text"]

    _, scaffold = await api.get("/api/scaffold", path=api.path)
    assert "helpers.py" in scaffold["files"]


# --------------------------------------------------------------------------
# Confinement
# --------------------------------------------------------------------------

@pytest.mark.parametrize("target", ["../../../etc/passwd", "../escape.py"])
async def test_reads_and_writes_stay_inside_the_project(api, target):
    status, _ = await api.get("/api/file", path=api.path, file=target)
    assert status == 403
    status, _ = await api.post("/api/file", path=api.path, file=target, text="x = 1")
    assert status == 403


# --------------------------------------------------------------------------
# Importing over HTTP
# --------------------------------------------------------------------------

async def test_detect_endpoint_opens_a_project(api):
    status, verdict = await api.get("/api/detect", path=api.path)
    assert status == 200
    assert verdict["is_project"] is True
    assert verdict["counts"]["neurons"] == 1


async def test_detect_endpoint_refuses_with_a_reason(api, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    status, verdict = await api.get("/api/detect", path=str(empty))
    assert status == 200            # a verdict, not an error
    assert verdict["is_project"] is False
    assert verdict["reason"]
    assert verdict["children"] == []


async def test_detect_endpoint_offers_child_projects(api):
    # api.path is the project; its parent is the folder holding it.
    parent = str(api.root.parent)
    status, verdict = await api.get("/api/detect", path=parent)
    assert status == 200
    assert verdict["is_project"] is False
    assert api.root.name in {c["name"] for c in verdict["children"]}


async def test_warnings_clear_once_they_stop_being_true(api):
    (api.root / "brain.py").unlink()
    _, verdict = await api.get("/api/detect", path=api.path)
    assert any(w["id"] == "no-brain" for w in verdict["warnings"])

    (api.root / "brain.py").write_text("from cosmonapse import Dendrite\n", encoding="utf-8")
    _, verdict = await api.get("/api/detect", path=api.path)
    assert not any(w["id"] == "no-brain" for w in verdict["warnings"])
