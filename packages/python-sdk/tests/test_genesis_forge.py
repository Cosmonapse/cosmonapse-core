"""
The git account behind Genesis' "clone a project" tab.

The thing worth protecting here is a promise rather than a behaviour:
**Genesis never stores the token.** A token pasted into the connect form goes
to `git credential approve` and nowhere else; the prefs file Genesis writes
holds a host and a login and is asserted, in this file, not to contain the
secret. If that ever stops being true it should stop being true loudly.

Everything is hermetic. HOME is redirected, git's global config is a temp
file, and the forge's HTTP layer is stubbed - so no test here touches the
developer's keychain, their ~/.gitconfig, or the network.
"""

import json
import shutil

import pytest

from cosmo.commands import _genesis_forge as forge

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="these tests drive the real git binary",
)

#: Shaped like a GitHub token so the code paths that sniff the prefix
#: behave, but it is a string in a test file.
TOKEN = "ghp_pretend_this_is_a_real_token"  # noqa: S105 - not a real credential


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """A machine of our own: empty home, empty git config, no network."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    empty = tmp_path / "gitconfig"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

    prefs = home / ".cosmonapse" / "genesis.json"
    monkeypatch.setattr(forge, "PREFS_DIR", prefs.parent)
    monkeypatch.setattr(forge, "PREFS_FILE", prefs)

    async def no_network(kind, base, token, path, params=None):
        raise AssertionError(f"unexpected API call to {path}")

    monkeypatch.setattr(forge, "_api", no_network)

    class Sandbox:
        home_dir = home
        prefs_file = prefs
        gitconfig = empty

        def api(self, handler):
            monkeypatch.setattr(forge, "_api", handler)

    return Sandbox()


def _github_api(user="ada", repos=()):
    async def handler(kind, base, token, path, params=None):
        assert token == TOKEN
        if path == "/user":
            return {"login": user}
        if path == "/user/repos":
            return list(repos)
        raise AssertionError(f"unexpected path {path}")
    return handler


# --------------------------------------------------------------------------
# Before anything is connected
# --------------------------------------------------------------------------

async def test_a_machine_with_no_helper_says_so_rather_than_offering_to_connect(sandbox):
    body = await forge.status()
    assert body["connected"] is False
    assert body["helpers"] == []
    assert body["can_store"] is True
    assert "nowhere for a token to be stored" in body["reason"]


async def test_connecting_without_a_helper_is_refused_by_default(sandbox):
    """`git credential approve` with no helper succeeds and stores nothing.
    Accepting the token there would look like it worked and fail at the first
    push, a long way from the form that caused it."""
    sandbox.api(_github_api())
    with pytest.raises(forge.ForgeError) as e:
        await forge.connect("github", TOKEN)
    assert "no credential helper" in str(e.value)
    assert not sandbox.prefs_file.exists()


# --------------------------------------------------------------------------
# Connecting
# --------------------------------------------------------------------------

async def test_connect_stores_the_token_in_git_and_not_in_genesis(sandbox):
    """The promise this module is built on, asserted from both sides."""
    sandbox.api(_github_api(user="ada"))
    body = await forge.connect("github", TOKEN, enable_store=True)

    assert body["connected"] is True
    assert body["host"] == "github.com"
    assert body["login"] == "ada"

    # git has it...
    stored = (sandbox.home_dir / ".git-credentials").read_text(encoding="utf-8")
    assert TOKEN in stored

    # ...and Genesis' own file does not, anywhere in it.
    prefs = sandbox.prefs_file.read_text(encoding="utf-8")
    assert TOKEN not in prefs
    assert json.loads(prefs)["forge"] == {
        "kind": "github",
        "base_url": "https://api.github.com",
        "login": "ada",
    }


async def test_the_credential_is_keyed_on_the_clone_host_not_the_api_host(sandbox):
    """api.github.com serves the API; github.com is what a clone URL says, and
    the credential store is keyed on the latter. Getting this wrong gives you
    a token that saves and is then never found."""
    sandbox.api(_github_api())
    await forge.connect("github", TOKEN, enable_store=True)
    stored = (sandbox.home_dir / ".git-credentials").read_text(encoding="utf-8")
    assert "@github.com" in stored
    assert "api.github.com" not in stored


async def test_a_rejected_token_is_never_stored(sandbox):
    """Validate before storing: a bad token in the keychain fails later as an
    auth error on push, which is much harder to trace back to this form."""
    async def rejects(kind, base, token, path, params=None):
        raise forge.ForgeError("That token was rejected.")
    sandbox.api(rejects)

    with pytest.raises(forge.ForgeError):
        await forge.connect("github", TOKEN, enable_store=True)
    assert not (sandbox.home_dir / ".git-credentials").exists()
    assert not sandbox.prefs_file.exists()


async def test_an_unknown_host_needs_a_base_url(sandbox):
    with pytest.raises(forge.ForgeError) as e:
        await forge.connect("other", TOKEN, enable_store=True)
    assert "base URL is required" in str(e.value)


async def test_a_self_hosted_gitlab_connects_on_its_own_address(sandbox):
    async def handler(kind, base, token, path, params=None):
        assert base == "https://git.example.com"
        return {"username": "grace"}
    sandbox.api(handler)

    body = await forge.connect(
        "gitlab", TOKEN, base_url="https://git.example.com/", enable_store=True,
    )
    assert body["host"] == "git.example.com"
    assert body["login"] == "grace"


# --------------------------------------------------------------------------
# Staying connected, and stopping
# --------------------------------------------------------------------------

async def test_status_stops_claiming_an_account_once_the_token_is_gone(sandbox):
    """Prefs alone are not evidence. If the user revokes the credential in
    their keychain, the panel has to notice rather than keep saying "ada"."""
    sandbox.api(_github_api())
    assert (await forge.connect("github", TOKEN, enable_store=True))["connected"]

    (sandbox.home_dir / ".git-credentials").write_text("", encoding="utf-8")

    body = await forge.status()
    assert body["connected"] is False
    assert "no longer has a token" in body["reason"]


async def test_disconnect_forgets_the_prefs_and_drops_the_credential(sandbox):
    sandbox.api(_github_api())
    await forge.connect("github", TOKEN, enable_store=True)

    body = await forge.disconnect()
    assert body["connected"] is False
    assert not sandbox.prefs_file.exists()
    stored = (sandbox.home_dir / ".git-credentials").read_text(encoding="utf-8")
    assert TOKEN not in stored


# --------------------------------------------------------------------------
# The repo picker
# --------------------------------------------------------------------------

async def test_repos_are_normalised_across_hosts(sandbox):
    """GitHub and GitLab disagree about every field name. The picker should
    not have to know that, so the shape is flattened here."""
    sandbox.api(_github_api(user="ada", repos=[
        {
            "name": "brainy", "full_name": "ada/brainy",
            "clone_url": "https://github.com/ada/brainy.git",
            "ssh_url": "git@github.com:ada/brainy.git",
            "private": True, "description": "a brain",
            "default_branch": "main", "pushed_at": "2026-08-01T00:00:00Z",
        },
    ]))
    await forge.connect("github", TOKEN, enable_store=True)

    body = await forge.repos()
    assert body["host"] == "github.com"
    assert body["repos"] == [{
        "name": "brainy",
        "full_name": "ada/brainy",
        "url": "https://github.com/ada/brainy.git",
        "ssh_url": "git@github.com:ada/brainy.git",
        "private": True,
        "description": "a brain",
        "default_branch": "main",
        "updated_at": "2026-08-01T00:00:00Z",
    }]


async def test_repos_can_be_searched(sandbox):
    sandbox.api(_github_api(repos=[
        {"name": "brainy", "full_name": "ada/brainy", "description": ""},
        {"name": "notes", "full_name": "ada/notes", "description": "a brain"},
        {"name": "website", "full_name": "ada/website", "description": ""},
    ]))
    await forge.connect("github", TOKEN, enable_store=True)

    found = await forge.repos("brain")
    # Matches the name of one and the description of another.
    assert {r["name"] for r in found["repos"]} == {"brainy", "notes"}


async def test_repos_without_an_account_says_to_connect(sandbox):
    with pytest.raises(forge.ForgeError):
        await forge.repos()


async def test_a_host_with_no_api_is_told_to_paste_a_url(sandbox):
    await forge.connect(
        "other", TOKEN, base_url="https://git.example.com", enable_store=True,
    )
    with pytest.raises(forge.ForgeError) as e:
        await forge.repos()
    assert "paste a clone URL" in str(e.value)


# --------------------------------------------------------------------------
# Through the routes
# --------------------------------------------------------------------------
#
# The tests above call the module. These two drive the handlers, because the
# wiring is where a rename quietly stops working and every unit test still
# passes - and because one of them checks something the module alone cannot:
# that no endpoint hands the token back out.

async def test_the_account_endpoints_are_wired(sandbox):
    from aiohttp.test_utils import TestClient, TestServer

    from cosmo.commands._genesis import build_app

    sandbox.api(_github_api(user="ada"))
    client = TestClient(TestServer(build_app(None)))
    await client.start_server()
    try:
        r = await client.get("/api/forge")
        assert r.status == 200
        assert (await r.json())["connected"] is False

        r = await client.post("/api/forge/connect", json={
            "kind": "github", "token": TOKEN, "enable_store": True,
        })
        assert r.status == 200, await r.text()
        assert (await r.json())["login"] == "ada"

        r = await client.post("/api/forge/disconnect", json={})
        assert r.status == 200
        assert (await r.json())["connected"] is False
    finally:
        await client.close()


async def test_no_endpoint_ever_returns_the_token(sandbox):
    """The whole arrangement is pointless if the secret comes back out over
    localhost. Asserted against the raw response text, not the parsed body,
    so a stray field anywhere in the tree still trips it."""
    from aiohttp.test_utils import TestClient, TestServer

    from cosmo.commands._genesis import build_app

    sandbox.api(_github_api(user="ada", repos=[
        {"name": "brainy", "full_name": "ada/brainy", "description": ""},
    ]))
    client = TestClient(TestServer(build_app(None)))
    await client.start_server()
    try:
        r = await client.post("/api/forge/connect", json={
            "kind": "github", "token": TOKEN, "enable_store": True,
        })
        assert TOKEN not in await r.text()

        for route in ("/api/forge", "/api/forge/repos"):
            r = await client.get(route)
            assert r.status == 200, route
            assert TOKEN not in await r.text(), route
    finally:
        await client.close()


async def test_a_refused_connect_is_a_409_not_a_500(sandbox):
    from aiohttp.test_utils import TestClient, TestServer

    from cosmo.commands._genesis import build_app

    client = TestClient(TestServer(build_app(None)))
    await client.start_server()
    try:
        # No helper on this machine and no explicit opt-in: the module raises,
        # and the handler has to turn that into something the form can show.
        r = await client.post("/api/forge/connect", json={
            "kind": "github", "token": TOKEN,
        })
        assert r.status == 409
        assert "credential helper" in (await r.json())["error"]
    finally:
        await client.close()
