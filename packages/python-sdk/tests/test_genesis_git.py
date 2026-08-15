"""
Genesis' git panel, driven the way the UI drives it.

Version control is the undo for everything Genesis does that `_archive/`
doesn't cover: an "add component" writes a module *and* rewrites brain.py,
and every Code-tab edit replaces a declaration in place. So these tests care
about two things above all else.

The first is that the panel tells the truth about a *Genesis* edit, not just
about a file somebody typed into - hence the test that adds a component
through /api/component and then asks git what happened.

The second is that the destructive end stays narrow. `restore` may only ever
touch the one path it was given, and may never invent a meaning for a file
git has no version of. A one-click undo that can revert a file you weren't
looking at is worse than no undo at all.

Hermetic on purpose: the fixture points git at empty config files, so no
developer's ~/.gitconfig can make "this machine has no identity yet" pass
locally and fail in CI, or the reverse.
"""

import ast
import asyncio
import pathlib
import shutil

import pytest
from aiohttp.test_utils import TestClient, TestServer

from cosmo.commands._genesis import build_app
from cosmo.commands.init import scaffold_project

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="these tests drive the real git binary",
)


@pytest.fixture
def _no_ambient_git_config(tmp_path, monkeypatch):
    """Cut every test off from the machine's own git configuration.

    `user.name` being set or unset changes what `init` is allowed to do, and
    inheriting that from whoever is running the suite makes half these tests
    pass or fail for reasons that have nothing to do with the code.
    """
    empty = tmp_path / "empty-gitconfig"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
async def api(tmp_path, _no_ambient_git_config):
    """A running Genesis API plus a scaffolded, not-yet-versioned project."""
    target, _ = scaffold_project(str(tmp_path / "my-app"), namespace="demo")
    client = TestClient(TestServer(build_app(None)))
    await client.start_server()

    class Api:
        path = str(target)
        root = target

        # `route` rather than `url`, because half these calls send a body
        # with a `url` key in it and a collision here is a TypeError with a
        # baffling message.
        async def get(self, route, **params):
            r = await client.get(route, params=params)
            return r.status, await r.json()

        async def post(self, route, **body):
            r = await client.post(route, json=body)
            return r.status, await r.json()

        async def start(self, **kw):
            """init + identity + first commit: the state most tests need."""
            status, body = await self.post("/api/git/init", path=self.path, **kw)
            assert status == 200, body
            status, body = await self.post(
                "/api/git/identity", path=self.path,
                name="Ada Lovelace", email="ada@example.com",
            )
            assert status == 200, body
            if body["unborn"]:
                status, body = await self.post(
                    "/api/git/commit", path=self.path,
                    message="Initial commit", stage_all=True,
                )
                assert status == 200, body
            return body

    yield Api()
    await client.close()


def _by_file(status, name):
    return next((f for f in status["files"] if f["rel"] == name), None)


# --------------------------------------------------------------------------
# Before there is a repository
# --------------------------------------------------------------------------

async def test_a_project_with_no_repo_is_an_answer_not_an_error(api):
    """The panel has to be able to *offer* to start one, so this is a 200."""
    status, body = await api.get("/api/git", path=api.path)
    assert status == 200
    assert body["available"] is True
    assert body["repo"] is False
    assert body["files"] == []
    assert "not in a git repository" in body["reason"]


async def test_status_needs_a_path(api):
    status, body = await api.get("/api/git")
    assert status == 400
    assert "path is required" in body["error"]


# --------------------------------------------------------------------------
# Starting one
# --------------------------------------------------------------------------

async def test_init_writes_a_gitignore_and_starts_a_repo(api):
    status, body = await api.post("/api/git/init", path=api.path)
    assert status == 200, body
    assert body["repo"] is True
    assert (api.root / ".gitignore").is_file()

    ignored = (api.root / ".gitignore").read_text(encoding="utf-8")
    # The two that actually matter: secrets, and the archive Genesis writes.
    assert ".env" in ignored
    assert "_archive/" in ignored


async def test_init_stops_short_of_committing_with_no_identity(api):
    """A repo with nobody to attribute a commit to is still worth having.

    Failing the whole init here would leave the user with nothing - no repo,
    no .gitignore - over a two-line fix the panel can offer inline.
    """
    status, body = await api.post("/api/git/init", path=api.path)
    assert status == 200, body
    assert body["unborn"] is True
    assert body["head"] is None
    assert body["identity"] is None
    assert "user.name" in body["note"]


async def test_identity_is_set_on_this_repo_only(api):
    await api.post("/api/git/init", path=api.path)
    status, body = await api.post(
        "/api/git/identity", path=api.path,
        name="Ada Lovelace", email="ada@example.com",
    )
    assert status == 200, body
    assert body["identity"] == {"name": "Ada Lovelace", "email": "ada@example.com"}
    # Written to the repo's own config, never --global.
    assert "ada@example.com" in (api.root / ".git" / "config").read_text(
        encoding="utf-8",
    )


async def test_init_refuses_to_nest_a_second_repository(api):
    await api.start()
    status, body = await api.post("/api/git/init", path=api.path)
    assert status == 409
    assert "already inside the git repository" in body["error"]


async def test_the_first_commit_lands_on_main(api):
    body = await api.start()
    assert body["branch"] == "main"
    assert body["head"]["subject"]
    assert body["clean"] is True


# --------------------------------------------------------------------------
# What Genesis' own edits look like
# --------------------------------------------------------------------------

async def test_adding_a_component_shows_up_as_a_new_file_and_a_changed_brain(api):
    """The reason the panel exists.

    Adding a Neuron is two changes, and the second one - brain.py being
    rewritten to import and attach it - is the half people don't expect.
    If the panel only showed the new module it would be quietly lying about
    what just happened to the project.
    """
    await api.start()
    status, body = await api.post(
        "/api/component", path=api.path, kind="neuron", name="summarize",
    )
    assert status == 200, body

    status, git = await api.get("/api/git", path=api.path)
    assert status == 200
    assert git["clean"] is False

    module = _by_file(git, "neurons/summarize.py")
    brain = _by_file(git, "brain.py")
    assert module is not None and module["untracked"] is True
    assert brain is not None and brain["label"] == "modified"


async def test_a_checkpoint_commits_everything_an_edit_touched(api):
    await api.start()
    await api.post("/api/component", path=api.path, kind="neuron", name="summarize")

    status, body = await api.post(
        "/api/git/commit", path=api.path,
        message="Add the summarize neuron", stage_all=True,
    )
    assert status == 200, body
    assert body["clean"] is True
    assert body["committed"]["subject"] == "Add the summarize neuron"

    status, log = await api.get("/api/git/log", path=api.path)
    assert log["commits"][0]["subject"] == "Add the summarize neuron"
    assert log["commits"][-1]["root"] is True


# --------------------------------------------------------------------------
# Staging
# --------------------------------------------------------------------------

async def test_staging_then_unstaging_one_file(api):
    await api.start()
    (api.root / "brain.py").write_text("# edited\n", encoding="utf-8")

    status, body = await api.post(
        "/api/git/stage", path=api.path, files=["brain.py"], staged=True,
    )
    assert status == 200, body
    assert _by_file(body, "brain.py")["staged"] is True
    assert body["staged"] == 1

    status, body = await api.post(
        "/api/git/stage", path=api.path, files=["brain.py"], staged=False,
    )
    assert status == 200, body
    assert _by_file(body, "brain.py")["staged"] is False


async def test_stage_wants_a_list(api):
    await api.start()
    status, body = await api.post("/api/git/stage", path=api.path, files="brain.py")
    assert status == 400
    assert "must be a list" in body["error"]


# --------------------------------------------------------------------------
# Committing
# --------------------------------------------------------------------------

async def test_commit_refuses_an_empty_message(api):
    await api.start()
    (api.root / "brain.py").write_text("# edited\n", encoding="utf-8")
    status, body = await api.post(
        "/api/git/commit", path=api.path, message="   ", stage_all=True,
    )
    assert status == 400
    assert "message is required" in body["error"]


async def test_commit_refuses_an_empty_index(api):
    """Not an exception to swallow: a clean tree means the button did nothing,
    and saying so is more useful than an empty commit nobody asked for."""
    await api.start()
    status, body = await api.post(
        "/api/git/commit", path=api.path, message="nothing to see",
    )
    assert status == 409
    assert "Nothing is staged" in body["error"]


async def test_commit_without_an_identity_says_which_two_things_are_missing(api):
    await api.post("/api/git/init", path=api.path)
    status, body = await api.post(
        "/api/git/commit", path=api.path, message="anything", stage_all=True,
    )
    assert status == 409
    assert "user.name" in body["error"] and "user.email" in body["error"]


# --------------------------------------------------------------------------
# Diffs
# --------------------------------------------------------------------------

async def test_diff_of_a_modified_file(api):
    await api.start()
    (api.root / "brain.py").write_text("# replaced\n", encoding="utf-8")
    status, body = await api.get("/api/git/diff", path=api.path, file="brain.py")
    assert status == 200, body
    assert body["empty"] is False
    assert "+# replaced" in body["diff"]
    assert body["rel"] == "brain.py"


async def test_diff_of_an_untracked_file_is_all_additions(api):
    """git itself has nothing to say about an untracked path, so the module
    synthesises the diff. It has to look like the one you'd get after
    staging, or the panel needs a second renderer for one case."""
    await api.start()
    (api.root / "notes.py").write_text("A = 1\nB = 2\n", encoding="utf-8")
    status, body = await api.get("/api/git/diff", path=api.path, file="notes.py")
    assert status == 200, body
    assert body["binary"] is False
    assert "+A = 1" in body["diff"]
    assert "+B = 2" in body["diff"]
    assert "--- /dev/null" in body["diff"]


async def test_diff_refuses_a_path_outside_the_repository(api):
    await api.start()
    status, body = await api.get(
        "/api/git/diff", path=api.path, file="../../../etc/passwd",
    )
    assert status == 403
    assert "escapes the repository" in body["error"]


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------

async def test_show_returns_the_header_the_stat_and_the_diff(api):
    await api.start()
    (api.root / "brain.py").write_text("# second\n", encoding="utf-8")
    _, committed = await api.post(
        "/api/git/commit", path=api.path, message="Second", stage_all=True,
    )
    sha = committed["committed"]["sha"]

    status, body = await api.get("/api/git/show", path=api.path, sha=sha)
    assert status == 200, body
    assert body["commit"]["subject"] == "Second"
    assert [c["file"] for c in body["changes"]] == ["brain.py"]
    assert "+# second" in body["diff"]


async def test_show_refuses_anything_that_is_not_a_plain_commit_id(api):
    """`HEAD~1..HEAD` is a perfectly good revision - and means something
    quite different from "this commit", which is all the panel ever means."""
    await api.start()
    status, body = await api.get("/api/git/show", path=api.path, sha="HEAD~1..HEAD")
    assert status == 400
    assert "not a commit id" in body["error"]


async def test_log_can_be_narrowed_to_one_file(api):
    await api.start()
    await api.post("/api/component", path=api.path, kind="neuron", name="summarize")
    await api.post("/api/git/commit", path=api.path,
                   message="Add summarize", stage_all=True)

    status, body = await api.get(
        "/api/git/log", path=api.path, file="neurons/summarize.py",
    )
    assert status == 200, body
    assert [c["subject"] for c in body["commits"]] == ["Add summarize"]


async def test_log_on_an_unborn_branch_is_empty_rather_than_an_error(api):
    await api.post("/api/git/init", path=api.path)
    status, body = await api.get("/api/git/log", path=api.path)
    assert status == 200, body
    assert body["commits"] == []


# --------------------------------------------------------------------------
# Restore - the destructive end
# --------------------------------------------------------------------------

async def test_restore_puts_one_file_back_and_leaves_the_others_alone(api):
    """The invariant that makes a one-click undo safe to offer at all."""
    await api.start()
    original = (api.root / "brain.py").read_text(encoding="utf-8")
    (api.root / "brain.py").write_text("# clobbered\n", encoding="utf-8")
    (api.root / "config.py").write_text("# also edited\n", encoding="utf-8")

    status, body = await api.post(
        "/api/git/restore", path=api.path, file="brain.py",
    )
    assert status == 200, body
    assert body["restored"] == {"file": "brain.py", "from": "HEAD"}
    assert (api.root / "brain.py").read_text(encoding="utf-8") == original
    # Untouched: restore is path-scoped, and the neighbour's edit survives.
    assert (api.root / "config.py").read_text(encoding="utf-8") == "# also edited\n"


async def test_restore_can_reach_back_to_an_older_commit(api):
    await api.start()
    first = (await api.get("/api/git/log", path=api.path))[1]["commits"][0]["sha"]
    original = (api.root / "brain.py").read_text(encoding="utf-8")

    (api.root / "brain.py").write_text("# second\n", encoding="utf-8")
    await api.post("/api/git/commit", path=api.path,
                   message="Second", stage_all=True)

    status, body = await api.post(
        "/api/git/restore", path=api.path, file="brain.py", sha=first,
    )
    assert status == 200, body
    assert (api.root / "brain.py").read_text(encoding="utf-8") == original


async def test_restore_refuses_a_file_git_has_never_seen(api):
    """"Restore" on something with no committed version can only mean
    "delete", and deleting a file the user has never saved anywhere is not a
    thing a button should infer."""
    await api.start()
    (api.root / "scratch.py").write_text("keep me\n", encoding="utf-8")
    status, body = await api.post(
        "/api/git/restore", path=api.path, file="scratch.py",
    )
    assert status == 409
    assert "no version of it to restore" in body["error"]
    assert (api.root / "scratch.py").is_file()


async def test_restore_refuses_a_path_outside_the_repository(api):
    await api.start()
    status, body = await api.post(
        "/api/git/restore", path=api.path, file="../../secrets.txt",
    )
    assert status == 403


# --------------------------------------------------------------------------
# A project inside a bigger repository
# --------------------------------------------------------------------------

async def test_a_project_in_a_subdirectory_reports_both_paths(tmp_path,
                                                              _no_ambient_git_config):
    """Status is repo-wide, because `git commit` is.

    Showing only the project's files while the commit button commits the
    whole index would mean the panel understated what the button did. So
    every row carries both spellings: `file` is what git calls it, `rel` is
    what it is called inside the project - and null for anything outside,
    which is how the UI knows to group it separately.
    """
    repo = tmp_path / "monorepo"
    repo.mkdir()
    target, _ = scaffold_project(str(repo / "services" / "my-app"), namespace="demo")
    (repo / "README.md").write_text("# monorepo\n", encoding="utf-8")

    client = TestClient(TestServer(build_app(None)))
    await client.start_server()
    try:
        async def post(url, **body):
            r = await client.post(url, json=body)
            return r.status, await r.json()

        status, body = await post("/api/git/init", path=str(repo))
        assert status == 200, body

        r = await client.get("/api/git", params={"path": str(target)})
        body = await r.json()
        assert body["prefix"] == "services/my-app/"
        assert body["root"] == str(repo.resolve())

        inside = next(f for f in body["files"] if f["file"].endswith("brain.py"))
        assert inside["file"] == "services/my-app/brain.py"
        assert inside["rel"] == "brain.py"

        outside = next(f for f in body["files"] if f["file"] == "README.md")
        assert outside["rel"] is None
    finally:
        await client.close()


# --------------------------------------------------------------------------
# The structural guard on the networked half
# --------------------------------------------------------------------------
#
# Networked verbs exist now, so "the module never says push" is no longer the
# invariant. What replaced it is narrower and more useful: every verb that can
# reach the network goes through `_run_net`, which is the only place the
# no-prompt guards and the long timeout are applied. If someone adds a fetch
# through plain `_run` one day, the guards silently stop covering it - and a
# handler that hangs on an ssh passphrase prompt is precisely the bug this
# whole arrangement exists to prevent. So it is asserted rather than trusted.

_NETWORKED = {"clone", "push", "pull", "fetch", "ls-remote", "submodule"}


def _first_string_arg(call):
    for arg in call.args[1:]:            # arg 0 is the cwd
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
        break
    return None


def test_every_networked_verb_goes_through_run_net():
    from cosmo.commands import _genesis_git as gg

    source = pathlib.Path(gg.__file__).with_suffix(".py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else None
        if name != "_run":
            continue
        verb = _first_string_arg(node)
        if verb in _NETWORKED:
            offenders.append((verb, node.lineno))
    assert not offenders, (
        f"networked verbs called through _run instead of _run_net: {offenders}"
    )


def test_the_no_prompt_guards_are_still_in_place():
    """Each of these is one deadlock. GIT_TERMINAL_PROMPT stops a credential
    helper asking for a password; BatchMode stops ssh asking for a key
    passphrase; accept-new stops it asking about an unknown host - while
    still refusing a host key that *changed*, which is the case worth
    refusing."""
    from cosmo.commands import _genesis_git as gg

    env = gg._env()
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "BatchMode=yes" in gg._SSH_BATCH
    assert "StrictHostKeyChecking=accept-new" in gg._SSH_BATCH
    assert "StrictHostKeyChecking=no" not in gg._SSH_BATCH
    assert gg._NET_TIMEOUT_S > gg._TIMEOUT_S


# --------------------------------------------------------------------------
# Scaffolding straight into a repository
# --------------------------------------------------------------------------

async def test_new_brain_form_can_start_a_repository(tmp_path,
                                                     _no_ambient_git_config):
    """The checkbox on the new-brain form, end to end."""
    client = TestClient(TestServer(build_app(None)))
    await client.start_server()
    try:
        r = await client.post("/api/init", json={
            "name": "fresh", "path": str(tmp_path), "namespace": "demo",
            "git": True,
        })
        assert r.status == 200, await r.text()
        body = await r.json()
        assert ".gitignore" in body["written"]
        assert body["git"]["repo"] is True
        assert body["git"]["branch"] == "main"
        assert (tmp_path / "fresh" / ".git").is_dir()
    finally:
        await client.close()


async def test_a_failed_repository_does_not_fail_the_scaffold(tmp_path,
                                                              _no_ambient_git_config):
    """Scaffolding into a folder already under version control.

    The project is written, the repository is not, and the reason travels
    back as a note. Rolling the scaffold back over this would throw away the
    thing the user actually asked for.
    """
    proc = await asyncio.create_subprocess_exec(
        "git", "init", "-q", cwd=str(tmp_path),
    )
    await proc.wait()

    client = TestClient(TestServer(build_app(None)))
    await client.start_server()
    try:
        r = await client.post("/api/init", json={
            "name": "fresh", "path": str(tmp_path), "namespace": "demo",
            "git": True,
        })
        assert r.status == 200, await r.text()
        body = await r.json()
        assert (tmp_path / "fresh" / "brain.py").is_file()
        assert body["git"]["repo"] is False
        assert "already inside the git repository" in body["git"]["note"]
    finally:
        await client.close()


# --------------------------------------------------------------------------
# Branches
# --------------------------------------------------------------------------

async def test_branches_lists_local_ones_and_marks_the_current(api):
    await api.start()
    status, body = await api.get("/api/git/branches", path=api.path)
    assert status == 200, body
    assert body["current"] == "main"
    assert [b["name"] for b in body["branches"]] == ["main"]
    assert body["branches"][0]["current"] is True


async def test_creating_a_branch_carries_uncommitted_work_across(api):
    """The most useful thing this button does: "I started typing, and now I
    want it somewhere of its own"."""
    await api.start()
    (api.root / "brain.py").write_text("# in progress\n", encoding="utf-8")

    status, body = await api.post(
        "/api/git/branch", path=api.path, name="feature/summarize", create=True,
    )
    assert status == 200, body
    assert body["branch"] == "feature/summarize"
    # The edit came with it rather than being stashed or lost.
    assert (api.root / "brain.py").read_text(encoding="utf-8") == "# in progress\n"
    assert body["clean"] is False


async def test_switching_to_an_existing_branch_is_refused_when_dirty(api):
    """Whether git carries the changes or refuses depends on what they touch.
    Nobody should have to predict that from a dropdown, so it is one rule."""
    await api.start()
    await api.post("/api/git/branch", path=api.path, name="other", create=True)
    await api.post("/api/git/branch", path=api.path, name="main")
    (api.root / "brain.py").write_text("# edited\n", encoding="utf-8")

    status, body = await api.post("/api/git/branch", path=api.path, name="other")
    assert status == 409
    assert "uncommitted changes" in body["error"]
    # And it really didn't move.
    _, after = await api.get("/api/git", path=api.path)
    assert after["branch"] == "main"


async def test_a_branch_name_git_would_reject_is_refused(api):
    await api.start()
    status, body = await api.post(
        "/api/git/branch", path=api.path, name="not a branch..", create=True,
    )
    assert status == 400
    assert "not a name git will accept" in body["error"]


# --------------------------------------------------------------------------
# Remotes, clone, push and pull
# --------------------------------------------------------------------------
#
# All of it against a bare repository on disk, reached over file://. No
# network, no credentials, and the code path is the same one a github.com URL
# takes - `clone`, `push` and `pull` do not know or care which it is.

@pytest.fixture
async def origin(tmp_path, _no_ambient_git_config):
    """A bare repository to act as the remote.

    Its HEAD is pointed at `main` explicitly. `git init --bare` picks
    `master` from git's built-in default, and a bare repo whose HEAD names a
    branch nobody ever pushes clones as an *empty working tree* with only a
    warning - which looks exactly like a clone bug and is not one.
    """
    bare = tmp_path / "origin.git"
    for args in (
        ("init", "--bare", "-q", str(bare)),
        ("-C", str(bare), "symbolic-ref", "HEAD", "refs/heads/main"),
    ):
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
    return bare


def _file_url(path) -> str:
    return "file://" + str(path).replace("\\", "/")


async def test_a_url_git_could_execute_is_refused(api):
    """`ext::sh -c ...` is a legal clone URL and a remote code execution. The
    allow-list is the reason a URL from a form is only ever a URL."""
    await api.start()
    for bad in ("ext::sh -c whoami", "--upload-pack=touch /tmp/x", "-oProxyCommand=x"):
        status, body = await api.post("/api/git/remote", path=api.path, url=bad)
        assert status == 400, (bad, body)


async def test_publishing_a_local_repo_then_pushing_it(api, origin):
    """The "I only have a local copy" path, end to end."""
    await api.start()

    status, body = await api.post(
        "/api/git/remote", path=api.path, url=_file_url(origin),
    )
    assert status == 200, body
    assert [r["name"] for r in body["remotes"]] == ["origin"]
    assert body["upstream"] is None

    status, body = await api.post("/api/git/push", path=api.path)
    assert status == 200, body
    # -u on the first push, so the branch now tracks and ahead/behind mean
    # something from here on.
    assert body["upstream"] == "origin/main"
    assert body["ahead"] == 0 and body["behind"] == 0


async def test_push_without_a_remote_says_what_to_do(api):
    await api.start()
    status, body = await api.post("/api/git/push", path=api.path)
    assert status == 409
    assert "no remote" in body["error"]


async def test_clone_lands_in_a_new_child_folder(api, origin, tmp_path):
    """Never into the selected folder itself. A clone unpacking over whatever
    the user had selected is a mistake with no undo."""
    await api.start()
    await api.post("/api/git/remote", path=api.path, url=_file_url(origin))
    await api.post("/api/git/push", path=api.path)

    into = tmp_path / "workspace"
    into.mkdir()
    status, body = await api.post(
        "/api/git/clone", path=str(into), url=_file_url(origin), name="pulled",
    )
    assert status == 200, body
    assert body["cloned_to"] == str(into / "pulled")
    assert (into / "pulled" / "brain.py").is_file()
    assert body["branch"] == "main"


async def test_clone_refuses_to_land_on_top_of_something(api, origin, tmp_path):
    await api.start()
    await api.post("/api/git/remote", path=api.path, url=_file_url(origin))
    await api.post("/api/git/push", path=api.path)

    into = tmp_path / "workspace"
    (into / "pulled").mkdir(parents=True)
    (into / "pulled" / "mine.txt").write_text("do not clobber me\n", encoding="utf-8")

    status, body = await api.post(
        "/api/git/clone", path=str(into), url=_file_url(origin), name="pulled",
    )
    assert status == 409
    assert "not empty" in body["error"]
    assert (into / "pulled" / "mine.txt").read_text(encoding="utf-8").strip() == (
        "do not clobber me"
    )


async def test_pull_fast_forwards_a_teammates_commit(api, origin, tmp_path):
    """Two clones of one repo: the second sees the first's work after a pull.

    This is the whole multi-developer story in one test - and it goes through
    the same `push` and `pull` a github.com remote would.
    """
    await api.start()
    await api.post("/api/git/remote", path=api.path, url=_file_url(origin))
    await api.post("/api/git/push", path=api.path)

    # A teammate clones, commits, pushes.
    into = tmp_path / "teammate"
    into.mkdir()
    _, cloned = await api.post(
        "/api/git/clone", path=str(into), url=_file_url(origin), name="app",
    )
    mate = cloned["cloned_to"]
    await api.post("/api/git/identity", path=mate,
                   name="Grace Hopper", email="grace@example.com")
    (pathlib.Path(mate) / "neurons" / "mate.py").write_text("Z = 1\n", encoding="utf-8")
    status, body = await api.post(
        "/api/git/commit", path=mate, message="A neuron from the other side",
        stage_all=True,
    )
    assert status == 200, body
    status, body = await api.post("/api/git/push", path=mate)
    assert status == 200, body

    # Back on the first copy: pull, and their commit is here.
    status, body = await api.post("/api/git/pull", path=api.path)
    assert status == 200, body
    assert body["head"]["subject"] == "A neuron from the other side"
    assert (api.root / "neurons" / "mate.py").is_file()
    assert body["behind"] == 0


async def test_a_diverged_branch_is_reported_not_merged(api, origin, tmp_path):
    """Merge resolution is out of scope, so a pull that cannot fast-forward
    has to say so plainly rather than quietly making a merge commit."""
    await api.start()
    await api.post("/api/git/remote", path=api.path, url=_file_url(origin))
    await api.post("/api/git/push", path=api.path)

    into = tmp_path / "teammate"
    into.mkdir()
    _, cloned = await api.post(
        "/api/git/clone", path=str(into), url=_file_url(origin), name="app",
    )
    mate = cloned["cloned_to"]
    await api.post("/api/git/identity", path=mate,
                   name="Grace Hopper", email="grace@example.com")
    (pathlib.Path(mate) / "theirs.py").write_text("T = 1\n", encoding="utf-8")
    await api.post("/api/git/commit", path=mate, message="Theirs", stage_all=True)
    await api.post("/api/git/push", path=mate)

    # ...and meanwhile, a commit of our own on the same branch.
    (api.root / "ours.py").write_text("O = 1\n", encoding="utf-8")
    await api.post("/api/git/commit", path=api.path, message="Ours", stage_all=True)

    status, body = await api.post("/api/git/push", path=api.path)
    assert status == 409
    assert "commits your branch does not" in body["error"]
    # The local commit survived the rejection - that is the promise the
    # message makes, so it is worth asserting rather than assuming.
    _, after = await api.get("/api/git", path=api.path)
    assert after["head"]["subject"] == "Ours"

    status, body = await api.post("/api/git/pull", path=api.path)
    assert status == 409
    assert "cannot be a fast-forward" in body["error"]
    assert "terminal" in body["error"]
