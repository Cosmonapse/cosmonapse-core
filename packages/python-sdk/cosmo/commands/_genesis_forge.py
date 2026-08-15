"""
cosmo.commands._genesis_forge
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The account half of Genesis' version control: who you are on GitHub or
GitLab, and which repositories that gets you.

This module is deliberately *not* about git. It knows how to check a token,
list the repositories it can see, and put it somewhere git will find it
later. Everything that actually runs the binary lives in ``_genesis_git``.
The split matters because these two have completely different failure modes -
one talks to a REST API over the internet, the other runs a local process -
and folding them together would mean one error path pretending to cover both.

Genesis never stores the token
------------------------------
This is the load-bearing decision. A token pasted into the connect form is
handed straight to ``git credential approve``, which writes it wherever the
user's ``credential.helper`` already keeps secrets: Windows Credential
Manager, the macOS Keychain, libsecret. Genesis keeps a small prefs file, and
that file holds a host and a login name - never the token. When this module
needs the token again (to list repositories) it asks git for it back with
``git credential fill``.

Three things follow from that, all of them good:

- Uninstalling Genesis leaves no secret behind, and revoking the token in
  the credential manager revokes it for Genesis too. There is one place to
  look, and it is the place the user already knows about.
- ``git push`` works without Genesis being involved at all. The credential
  it stored is the same one a terminal push finds, because it *is* the same
  store. Nothing here is a private side channel.
- A machine with no credential helper configured cannot silently half-work.
  It is detected up front (`credential_helpers`) and reported, so the UI can
  say so rather than storing a token into a void and failing at the first
  push.

Prompts are the enemy
---------------------
``GIT_TERMINAL_PROMPT=0`` is set on every git call here for the same reason
it is in ``_genesis_git``: a credential helper that decides to ask the user
something has no terminal to ask on, and would hang an aiohttp handler
forever. Failing immediately with a readable message is always the better
trade.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

#: Where the *non-secret* half of the connection is remembered: which host,
#: and who git said we are. Never the token. Under the user's home rather
#: than the project, because an account is a property of the person, not of
#: any one brain they are building.
PREFS_DIR = Path.home() / ".cosmonapse"
PREFS_FILE = PREFS_DIR / "genesis.json"

#: Ceiling on a `git credential` call. These are local and instant; anything
#: slower is a helper waiting on something it will never get.
_CRED_TIMEOUT_S = 20.0

#: Ceiling on one REST call to a forge.
_API_TIMEOUT_S = 30.0

#: How many repositories to pull back. One page is plenty for a picker with a
#: search box in it, and paginating to five hundred would make connecting feel
#: slower for something nobody scrolls through.
_REPO_PAGE = 100

#: The hosts with a repo listing implemented. "other" still connects and still
#: clones - it just has no API to enumerate, so the UI falls back to a URL
#: field. Being able to say "I do not know how to list this host" is better
#: than pretending every git server speaks GitHub.
KINDS = ("github", "gitlab", "other")

_DEFAULT_BASE = {
    "github": "https://api.github.com",
    "gitlab": "https://gitlab.com",
}

#: Where to send someone to make a token, and what to tick. Carried here
#: rather than in the UI because the scope names are part of the contract
#: with the API calls below - if those change, this changes with them.
TOKEN_HELP = {
    "github": {
        "url": "https://github.com/settings/tokens/new?scopes=repo&description=Cosmonapse%20Genesis",
        "scopes": "repo",
    },
    "gitlab": {
        "url": "https://gitlab.com/-/user_settings/personal_access_tokens",
        "scopes": "api, read_repository, write_repository",
    },
}


class ForgeError(RuntimeError):
    """Something the user should read, rather than a stack trace."""


# ---------------------------------------------------------------------------
# Prefs
# ---------------------------------------------------------------------------

def _read_prefs() -> dict[str, Any]:
    try:
        data = json.loads(PREFS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_prefs(data: dict[str, Any]) -> None:
    PREFS_DIR.mkdir(parents=True, exist_ok=True)
    PREFS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _clear_prefs() -> None:
    try:
        PREFS_FILE.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Talking to git's credential store
# ---------------------------------------------------------------------------

def _git_binary() -> str:
    exe = shutil.which("git")
    if exe is None:
        raise ForgeError(
            "git is not installed, or not on this machine's PATH. Install it "
            "from https://git-scm.com and reload Genesis.",
        )
    return exe


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = ""
    env["SSH_ASKPASS"] = ""
    return env


async def _credential(verb: str, fields: dict[str, str]) -> dict[str, str]:
    """Run one ``git credential <verb>`` and parse whatever it prints back.

    The protocol is a blank-line-terminated block of ``key=value`` in and the
    same out. ``fill`` answers with a username and password if the helper has
    one; ``approve`` and ``reject`` answer with nothing and are run for the
    side effect.
    """
    payload = "".join(f"{k}={v}\n" for k, v in fields.items()) + "\n"

    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    proc = await asyncio.create_subprocess_exec(
        _git_binary(), "credential", verb,
        # Home rather than any project: a repository's own config can define
        # a credential.helper, and which store a token lands in should not
        # depend on which brain happened to be open when it was pasted.
        cwd=str(Path.home()),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_env(),
        **kwargs,
    )
    try:
        out_b, err_b = await asyncio.wait_for(
            proc.communicate(payload.encode("utf-8")), _CRED_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        proc.kill()
        raise ForgeError(
            f"git credential {verb} did not answer within {_CRED_TIMEOUT_S:.0f}s. "
            f"Your credential helper is waiting on something - check it with "
            f"`git config --get-all credential.helper`.",
        )

    if proc.returncode != 0 and verb == "fill":
        # Not an error worth raising: "nothing stored for this host" is a
        # perfectly ordinary answer, and it is the one the caller wants.
        return {}
    if proc.returncode != 0:
        raise ForgeError(
            err_b.decode("utf-8", errors="replace").strip()
            or f"git credential {verb} failed.",
        )

    parsed: dict[str, str] = {}
    for line in out_b.decode("utf-8", errors="replace").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            parsed[key] = value
    return parsed


async def credential_helpers() -> list[str]:
    """Which credential helpers git is configured to use, if any.

    An empty list is the interesting case, and it is why this is checked
    before a token is ever accepted: with no helper, `git credential approve`
    succeeds and stores nothing, so the token would appear to save and then
    every push would ask for a password nobody can type.
    """
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = await asyncio.create_subprocess_exec(
        _git_binary(), "config", "--get-all", "credential.helper",
        cwd=str(Path.home()),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        stdin=asyncio.subprocess.DEVNULL,
        env=_env(),
        **kwargs,
    )
    out_b, _ = await proc.communicate()
    return [
        line.strip()
        for line in out_b.decode("utf-8", errors="replace").splitlines()
        if line.strip()
    ]


async def enable_store_helper() -> str:
    """Turn on git's plaintext ``store`` helper, globally, on request only.

    Offered as an explicit tick in the UI with the words "plaintext" next to
    it, and never reached for automatically. It exists because a Linux
    machine with no keyring is otherwise a dead end, and telling someone to
    go and configure a credential helper before they can clone is a worse
    answer than telling them exactly what the cheap one costs.
    """
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = await asyncio.create_subprocess_exec(
        _git_binary(), "config", "--global", "--add", "credential.helper", "store",
        cwd=str(Path.home()),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
        env=_env(),
        **kwargs,
    )
    _, err_b = await proc.communicate()
    if proc.returncode != 0:
        raise ForgeError(
            err_b.decode("utf-8", errors="replace").strip()
            or "Couldn't turn on git's credential store.",
        )
    return str(Path.home() / ".git-credentials")


# ---------------------------------------------------------------------------
# Talking to the forge
# ---------------------------------------------------------------------------

def _host_of(base_url: str) -> str:
    host = urlparse(base_url).hostname or ""
    # api.github.com is the API; github.com is what a clone URL says, and the
    # credential store is keyed on the latter. Getting this wrong is the
    # difference between a token that saves and a token that is never found.
    return "github.com" if host == "api.github.com" else host


def _base_url(kind: str, base_url: str = "") -> str:
    url = (base_url or "").strip().rstrip("/")
    if url:
        return url
    if kind not in _DEFAULT_BASE:
        raise ForgeError(
            f"{kind} has no default host, so a base URL is required - the "
            f"address of your git server, like https://git.example.com.",
        )
    return _DEFAULT_BASE[kind]


async def _api(
    kind: str, base: str, token: str, path: str, params: dict[str, str] | None = None,
) -> Any:
    """One GET against a forge's REST API, with its own auth header spelling."""
    import aiohttp

    if kind == "github":
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
    else:
        headers = {"PRIVATE-TOKEN": token}

    url = base + path
    timeout = aiohttp.ClientTimeout(total=_API_TIMEOUT_S)
    try:
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.get(url, headers=headers, params=params or {}) as resp,
        ):
            text = await resp.text()
            if resp.status == 401:
                raise ForgeError(
                    "That token was rejected. It may be expired, revoked, or "
                    "missing the scopes this needs.",
                )
            if resp.status == 403:
                raise ForgeError(
                    "That token is valid but not allowed to do this - most "
                    "often a missing scope, sometimes a rate limit.",
                )
            if resp.status >= 400:
                raise ForgeError(f"{url} answered {resp.status}: {text[:300]}")
            try:
                return json.loads(text)
            except ValueError:
                raise ForgeError(
                    f"{url} answered {resp.status} but not JSON. Is that the "
                    f"API address rather than the web address?",
                )
    except aiohttp.ClientError as e:
        raise ForgeError(
            f"Couldn't reach {url}: {type(e).__name__}: {e}. Check the address "
            f"and that this machine is online.",
        )
    except asyncio.TimeoutError:
        raise ForgeError(f"{url} did not answer within {_API_TIMEOUT_S:.0f}s.")


async def _whoami(kind: str, base: str, token: str) -> str:
    if kind == "github":
        data = await _api(kind, base, token, "/user")
        return str(data.get("login") or "")
    if kind == "gitlab":
        data = await _api(kind, base, token, "/api/v4/user")
        return str(data.get("username") or "")
    # "other": no API to ask, so the login is whatever the user typed. git
    # only needs *a* username to key the credential on; for a token most
    # hosts accept anything non-empty.
    return ""


# ---------------------------------------------------------------------------
# The account
# ---------------------------------------------------------------------------

def _offline(reason: str | None = None, **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "connected": False,
        "kind": None,
        "base_url": None,
        "host": None,
        "login": None,
        "helpers": [],
        "can_store": False,
        "token_help": TOKEN_HELP,
        "kinds": list(KINDS),
        "reason": reason,
    }
    base.update(extra)
    return base


async def status() -> dict[str, Any]:
    """Are we connected, and is there anywhere for a token to live?

    "Connected" means two separate things are both true: the prefs file
    remembers a host, *and* git's credential store still has something for
    it. Checking only the first would leave the UI claiming an account after
    the user revoked the token in their keychain, which is exactly the moment
    it must not lie.
    """
    if shutil.which("git") is None:
        return _offline(
            "git is not installed, or not on this machine's PATH. Install it "
            "from https://git-scm.com and reload Genesis.",
        )

    helpers = await credential_helpers()
    prefs = _read_prefs().get("forge") or {}
    kind = prefs.get("kind")
    if kind not in KINDS:
        return _offline(
            None if helpers else
            "git has no credential helper configured on this machine, so "
            "there is nowhere for a token to be stored.",
            helpers=helpers,
            can_store=not helpers,
        )

    base = str(prefs.get("base_url") or "")
    host = _host_of(base)
    cred = await _credential("fill", {"protocol": "https", "host": host})
    if not cred.get("password"):
        return _offline(
            f"Genesis remembers a {kind} account on {host}, but git's "
            f"credential store no longer has a token for it. Connect again.",
            helpers=helpers,
            can_store=not helpers,
        )

    return {
        "connected": True,
        "kind": kind,
        "base_url": base,
        "host": host,
        "login": prefs.get("login") or cred.get("username") or None,
        "helpers": helpers,
        "can_store": not helpers,
        "token_help": TOKEN_HELP,
        "kinds": list(KINDS),
        "reason": None,
    }


async def connect(
    kind: str, token: str, *, base_url: str = "", login: str = "",
    enable_store: bool = False,
) -> dict[str, Any]:
    """Check a token, store it in git's credential helper, remember the host.

    The order is deliberate: validate before storing. A token that does not
    work should never end up in the user's keychain, because the failure it
    causes later - a push rejected for auth - is much harder to trace back
    here than a message in the form they are standing in front of.
    """
    kind = (kind or "").strip().lower()
    if kind not in KINDS:
        raise ForgeError(f"{kind!r} is not a host Genesis knows how to connect to.")
    token = (token or "").strip()
    if not token:
        raise ForgeError("a token is required")

    base = _base_url(kind, base_url)
    host = _host_of(base)
    if not host:
        raise ForgeError(f"{base} is not a URL with a host in it.")

    helpers = await credential_helpers()
    if not helpers:
        if not enable_store:
            raise ForgeError(
                "git has no credential helper on this machine, so a token "
                "stored now would vanish and every push would ask for a "
                "password. Turn on git's plaintext store, or configure a "
                "helper yourself (Git Credential Manager, osxkeychain, "
                "libsecret) and try again.",
            )
        await enable_store_helper()

    who = await _whoami(kind, base, token) if kind != "other" else ""
    username = who or (login or "").strip() or "token"

    await _credential("approve", {
        "protocol": "https",
        "host": host,
        "username": username,
        "password": token,
    })

    prefs = _read_prefs()
    # The token is conspicuously not in here. See the module docstring.
    prefs["forge"] = {"kind": kind, "base_url": base, "login": username}
    _write_prefs(prefs)
    return await status()


async def disconnect() -> dict[str, Any]:
    """Forget the account, and ask git to drop the credential too.

    Both halves, because forgetting only the prefs would leave a working
    token in the user's keychain that Genesis put there and no longer
    mentions - which is precisely the sort of thing a tool should not leave
    lying around.
    """
    prefs = _read_prefs()
    forge = prefs.get("forge") or {}
    host = _host_of(str(forge.get("base_url") or ""))
    if host:
        try:
            await _credential("reject", {
                "protocol": "https",
                "host": host,
                "username": str(forge.get("login") or "token"),
            })
        except ForgeError:
            # A helper that will not drop it is not a reason to keep claiming
            # an account Genesis has already forgotten.
            pass
    prefs.pop("forge", None)
    if prefs:
        _write_prefs(prefs)
    else:
        _clear_prefs()
    return await status()


async def repos(query: str = "") -> dict[str, Any]:
    """The repositories this token can see, newest activity first.

    Filtered here rather than in the browser so that "search" means the same
    thing as the list grows past one page - today it is a substring match
    over a hundred entries, and when that stops being enough the change is
    one function rather than one function plus a component.
    """
    account = await status()
    if not account["connected"]:
        raise ForgeError(account["reason"] or "Not connected to a git account.")

    kind = account["kind"]
    base = account["base_url"]
    if kind == "other":
        raise ForgeError(
            "Genesis can store credentials for this host but has no way to "
            "list its repositories - paste a clone URL instead.",
        )

    cred = await _credential("fill", {"protocol": "https", "host": account["host"]})
    token = cred.get("password") or ""
    if not token:
        raise ForgeError("git's credential store no longer has a token for this host.")

    if kind == "github":
        raw = await _api(kind, base, token, "/user/repos", {
            "per_page": str(_REPO_PAGE),
            "sort": "updated",
            "affiliation": "owner,collaborator,organization_member",
        })
        found = [{
            "name": r.get("name") or "",
            "full_name": r.get("full_name") or "",
            "url": r.get("clone_url") or "",
            "ssh_url": r.get("ssh_url") or "",
            "private": bool(r.get("private")),
            "description": r.get("description") or "",
            "default_branch": r.get("default_branch") or "",
            "updated_at": r.get("pushed_at") or r.get("updated_at") or "",
        } for r in raw if isinstance(r, dict)]
    else:
        raw = await _api(kind, base, token, "/api/v4/projects", {
            "membership": "true",
            "per_page": str(_REPO_PAGE),
            "order_by": "last_activity_at",
        })
        found = [{
            "name": r.get("path") or "",
            "full_name": r.get("path_with_namespace") or "",
            "url": r.get("http_url_to_repo") or "",
            "ssh_url": r.get("ssh_url_to_repo") or "",
            "private": (r.get("visibility") or "private") != "public",
            "description": r.get("description") or "",
            "default_branch": r.get("default_branch") or "",
            "updated_at": r.get("last_activity_at") or "",
        } for r in raw if isinstance(r, dict)]

    needle = (query or "").strip().lower()
    if needle:
        found = [
            r for r in found
            if needle in r["full_name"].lower() or needle in r["description"].lower()
        ]
    return {"repos": found, "host": account["host"], "login": account["login"]}
