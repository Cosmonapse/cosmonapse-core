"""
cosmo.commands._genesis_git
~~~~~~~~~~~~~~~~~~~~~~~~~~~
Version control for the project Genesis is editing, over the user's own git.

Why Genesis needs one
---------------------
Genesis edits real source files, and most of what it does is structural. An
"add component" writes a module *and* rewrites brain.py; a "remove" takes
three separate things back out of it; every Code-tab edit is an AST rewrite
that replaces a whole declaration in place. ``_archive/`` catches exactly one
of those - a module you deleted on purpose - and nothing else. The undo for
the rest is the one developers already trust, so Genesis reaches for that
rather than inventing a second history nobody can inspect from a terminal.

Shelling out, not a library
---------------------------
Every call here runs the ``git`` binary the user already has, in their
project, with their config. That is the point: a repository Genesis touched
has to stay indistinguishable from one driven from a terminal - their
``user.name`` on the commits, their hooks, their ``.gitignore``, their
``core.autocrlf``. A bundled pure-Python implementation would be a second git
with subtly different opinions about all four, and the first time the two
disagreed the user would be debugging Genesis instead of their brain.

Networked, but only on purpose
------------------------------
Clone, fetch, push and pull live here too, and they are the only functions
that touch the network. Everything about them is arranged around one failure
mode: a credential helper or an ssh key that decides to *ask the user
something* has no terminal to ask on, and would hang an aiohttp handler
forever.

So networked verbs go through ``_run_net`` rather than ``_run``, which
differs in exactly three ways:

- ``GIT_TERMINAL_PROMPT=0`` (set for every call, networked or not) plus an
  ``ssh`` forced into ``BatchMode=yes``. Between them there is no path by
  which git stops and waits for a human.
- ``StrictHostKeyChecking=accept-new``, so a first-ever clone from a host
  does not deadlock on the "are you sure?" prompt - while a *changed* host
  key still fails loudly, which is the case that actually matters.
- A far longer timeout, because a clone is allowed to take minutes where a
  ``git status`` is not.

Credentials themselves are none of this module's business. They live in the
user's own credential helper, put there by ``_genesis_forge``, and git finds
them the same way it would for a push typed into a terminal.

What it deliberately will not do
--------------------------------
- **Nothing that merges.** ``pull`` is ``--ff-only``. A branch that has
  genuinely diverged needs a three-way merge and a conflict editor, and half
  of one is worse than sending you to a terminal that has a whole one.
- **Nothing that touches more than one file.** Restores are always
  path-scoped: ``git checkout <sha> -- one/file.py``, never a bare
  ``git checkout .``. A one-click undo that can silently revert a file you
  were not looking at is not an undo, it is a trap.
- **Nothing on its own.** Genesis never commits, pushes or pulls as a side
  effect of an edit. History written behind your back is history you cannot
  read, and it would race a user committing from a terminal in the same
  repo. The Git tab shows the dirty tree an edit left behind; turning that
  into a commit, and the commit into a push, stays two decisions.
- **Nothing that switches branches under uncommitted work.** Creating a
  branch from where you are carries your changes across, which is the point
  of it. Switching to an existing one with a dirty tree is refused, because
  the outcome depends on details of the diff that nobody should have to
  predict from a dropdown.

Scope: the repository, not the project folder
---------------------------------------------
Status is reported for the whole repository even when the project is a
subdirectory of something larger. Scoping the *view* to the project while
``git commit`` still commits the whole index would mean the panel showed less
than the button did, which is the one failure mode worth designing out. So
every entry carries ``rel`` - its path relative to the project, or null when
it lives elsewhere in the repo - and the UI groups on that instead.

The safety boundary is therefore the repo root, not the project directory:
a path handed to any function here must resolve inside the repository. That
is what makes ``file`` in a request a git path rather than an arbitrary one.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

#: Ceiling on one git invocation. Every command here is local and short; a
#: git that has not answered in this long is wedged (a stale index.lock, a
#: hook waiting on something), and hanging the handler forever is worse than
#: reporting it.
_TIMEOUT_S = 20.0

#: Ceiling on a networked invocation. Clones are allowed to take minutes;
#: the point of a bound at all is that a wedged transfer eventually reports
#: rather than pinning a handler until Genesis is restarted.
_NET_TIMEOUT_S = 600.0

#: ssh, with every question it could ask turned off. BatchMode stops it
#: prompting for a key passphrase (an agent-less key fails instead, which is
#: at least a message). accept-new takes the first host key silently but
#: still refuses a *changed* one - the difference between convenience and
#: turning off the check.
_SSH_BATCH = "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new"

#: How many commits the log returns by default.
DEFAULT_LOG_LIMIT = 60

#: Diffs are for reading, and past this nobody is reading. Truncated rather
#: than refused, because the head of a huge diff is still the useful part.
_MAX_DIFF_BYTES = 400_000

#: How much of an untracked file to render as an all-added diff.
_MAX_NEW_FILE_BYTES = 200_000

#: Unit separator / record separator. Neither can occur in a git object name,
#: an author line or a subject, which is what makes the log parseable without
#: guessing where one commit ends.
_US = "\x1f"
_RS = "\x1e"

_LOG_FORMAT = _US.join(["%H", "%h", "%an", "%ae", "%aI", "%s", "%P"]) + _RS

#: Two-letter status codes, said in words. The UI shows these rather than
#: "M"/"??" because the panel is aimed at someone who has not memorised
#: porcelain output.
_LABELS = {
    "M": "modified",
    "A": "added",
    "D": "deleted",
    "R": "renamed",
    "C": "copied",
    "T": "type changed",
    "U": "conflicted",
    "?": "untracked",
    "!": "ignored",
}

#: What `cosmo init` and the Git tab write when a project has no .gitignore.
#: Deliberately short: everything here is either generated by the toolchain
#: or a secret, and a long speculative ignore file is how real files end up
#: invisible.
GITIGNORE = """\
# Python
__pycache__/
*.py[cod]
.venv/
venv/
env/
.mypy_cache/
.pytest_cache/
.ruff_cache/

# Secrets - API keys for the models your Neurons call
.env
.env.*
!.env.example

# Genesis puts removed components here. They are recoverable from history
# once committed, so they do not need to be carried in the tree as well.
_archive/

# Local Engram state
*.sqlite
*.sqlite3
*.db

# Editors / OS
.DS_Store
.idea/
.vscode/
"""


class GitError(RuntimeError):
    """A git invocation that failed for a reason the user should read.

    ``stderr`` is carried separately from the message because the handlers
    put both on the wire: the message is what the panel shows, the raw
    output is what makes an unexpected failure diagnosable without going to
    a terminal.
    """

    def __init__(self, message: str, *, code: int = 1, stderr: str = "") -> None:
        self.code = code
        self.stderr = stderr.strip()
        super().__init__(message)


# ---------------------------------------------------------------------------
# Running git
# ---------------------------------------------------------------------------

def git_binary() -> str | None:
    """Absolute path to the user's git, or None if there isn't one.

    Resolved through PATH on every call rather than cached: Genesis can
    outlive an install, and "restart Genesis after installing git" is a
    worse answer than looking again.
    """
    return shutil.which("git")


def _env() -> dict[str, str]:
    env = dict(os.environ)
    # Belt and braces against a credential helper deciding to prompt: no
    # networked verb is ever issued, and if one somehow were, it fails fast
    # instead of blocking the event loop on a terminal that isn't there.
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = ""
    env["SSH_ASKPASS"] = ""
    # Read-only commands do not need to take index.lock, so a Genesis poll
    # can never collide with a `git add` the user is running in a terminal
    # on the same repo.
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


async def _run(
    cwd: Path, *args: str, check: bool = True,
    timeout: float = _TIMEOUT_S,  # noqa: ASYNC109 - wraps asyncio.wait_for, not a cancel scope
    net: bool = False,
) -> tuple[int, str, str]:
    """Run one git command in ``cwd`` and return (code, stdout, stderr).

    ``-c core.quotepath=false`` is what keeps non-ASCII paths readable: git's
    default octal-escapes them, and a path the UI cannot render is a file the
    user cannot stage. Colour is forced off for the same class of reason - a
    user with ``color.ui=always`` in their config would otherwise get ANSI
    escapes embedded in every diff the panel shows.
    """
    exe = git_binary()
    if exe is None:
        raise GitError(
            "git is not installed, or not on this machine's PATH. Install it "
            "from https://git-scm.com and reload Genesis.",
        )

    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        # git is a console program; without this a Genesis started from a
        # windowless launcher flashes a console for every poll.
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    env = _env()
    if net:
        # Only on networked calls, and only when the user has not already
        # said how ssh should run. Overriding a deliberate GIT_SSH_COMMAND
        # would break exactly the people who know what theirs is for.
        env.setdefault("GIT_SSH_COMMAND", _SSH_BATCH)

    # List-form argv, no shell. `exe` came from PATH lookup and every element
    # of `args` is built by this module - the only caller-supplied values are
    # pathspecs and a commit message, and both arrive after a literal `--` or
    # as the argument to `-m`.
    proc = await asyncio.create_subprocess_exec(
        exe, "--no-pager",
        "-c", "core.quotepath=false",
        "-c", "color.ui=false",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
        env=env,
        **kwargs,
    )
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise GitError(
            f"git {args[0] if args else ''} did not finish within "
            f"{timeout:.0f}s. " + (
                "The other end never answered - check the network, and that "
                "the remote address is right."
                if net else
                "Something is holding the repository - a stale "
                ".git/index.lock, or a hook waiting on input."
            ),
        )

    # errors="replace" everywhere: a repo can contain a file whose name is
    # not valid UTF-8, and a panel that shows a mangled name is better than
    # one that 500s on the whole status.
    out = out_b.decode("utf-8", errors="replace")
    err = err_b.decode("utf-8", errors="replace")
    code = proc.returncode or 0
    if check and code != 0:
        raise GitError(
            err.strip() or f"git {' '.join(args)} failed with exit code {code}.",
            code=code, stderr=err,
        )
    return code, out, err


async def _run_net(cwd: Path, *args: str, check: bool = True) -> tuple[int, str, str]:
    """A networked git call: same guards, no prompting, a much longer leash.

    Split from ``_run`` rather than parameterised into it so that the set of
    things that can reach the network is exactly the set of call sites that
    name this function. That is a property a reader can check, and a test
    does check it.
    """
    return await _run(cwd, *args, check=check, timeout=_NET_TIMEOUT_S, net=True)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _expanded(raw: str) -> Path:
    """``~`` expansion plus resolution, in one sync call. See `_resolved`."""
    return Path(raw).expanduser().resolve()


def _resolved(raw: str) -> Path:
    """``Path.resolve`` behind a sync function.

    Every caller here is an async handler, and resolving a path is a stat
    syscall - fast, local, and never worth an executor hop, but ruff's async
    rules are right to insist the blocking call is at least visible. Keeping
    the filesystem pokes in named sync helpers says "this blocks, and that is
    deliberate" once instead of at twelve call sites.
    """
    return Path(raw).resolve()


def _is_file(path: Path) -> bool:
    return path.is_file()


def _is_dir(path: Path) -> bool:
    return path.is_dir()


def _has_contents(path: Path) -> bool:
    try:
        return any(path.iterdir())
    except OSError:
        return False


def _project(raw_path: str) -> Path:
    target = Path(raw_path).expanduser().resolve()
    if not target.is_dir():
        raise FileNotFoundError(f"{target} is not a directory")
    return target


async def _root(target: Path) -> Path | None:
    """The repository ``target`` is inside, or None if it is not in one."""
    code, out, _ = await _run(target, "rev-parse", "--show-toplevel", check=False)
    if code != 0:
        return None
    line = out.strip()
    return _resolved(line) if line else None


def _in_repo(root: Path, file: str) -> str:
    """Validate one repo-relative path and hand it back in git's own spelling.

    The boundary is the repository, not the project: status legitimately
    reports files that live elsewhere in the repo, and refusing to diff one
    would make the panel show rows it cannot open. Everything outside the
    repo is still refused, which is what keeps ``file`` in a request from
    being an arbitrary path into the filesystem.
    """
    rel = (file or "").strip().replace("\\", "/")
    if not rel:
        raise ValueError("file is required")
    candidate = (root / rel).resolve()
    if candidate != root and root not in candidate.parents:
        raise PermissionError("path escapes the repository")
    return candidate.relative_to(root).as_posix()


def _relative_to_project(root: Path, target: Path, file: str) -> str | None:
    """A repo path re-expressed against the project, or None if it's outside.

    This is the whole of the "in this project / elsewhere in the repo" split
    the panel draws, kept on the server so both halves of it come from one
    piece of path arithmetic.
    """
    try:
        return (root / file).resolve().relative_to(target).as_posix()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def _offline(reason: str, **extra: Any) -> dict[str, Any]:
    """A status nobody can act on, with the reason spelled out for the panel."""
    base: dict[str, Any] = {
        "available": git_binary() is not None,
        "version": None,
        "repo": False,
        "root": None,
        "prefix": "",
        "branch": None,
        "detached": False,
        "unborn": False,
        "head": None,
        "files": [],
        "staged": 0,
        "unstaged": 0,
        "untracked": 0,
        "conflicted": 0,
        "clean": True,
        "identity": None,
        "remotes": [],
        "upstream": None,
        "ahead": 0,
        "behind": 0,
        "has_gitignore": False,
        "reason": reason,
    }
    base.update(extra)
    return base


def _parse_status(payload: str, root: Path, target: Path) -> list[dict[str, Any]]:
    """Parse ``status --porcelain -z`` into one row per path.

    ``-z`` rather than the newline form because a filename may contain a
    newline, and the difference only ever shows up on the repo of the person
    who reports the bug. Renames and copies emit their origin as a *second*
    NUL-terminated field with no marker of its own, so the parser has to
    consume it based on the code it just read - which is why this is a
    hand-rolled cursor and not a split().
    """
    fields = payload.split("\0")
    rows: list[dict[str, Any]] = []
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        index, worktree, path = entry[0], entry[1], entry[3:]
        origin: str | None = None
        if (index in ("R", "C") or worktree in ("R", "C")) and i < len(fields):
            origin = fields[i]
            i += 1

        untracked = index == "?"
        conflicted = index == "U" or worktree == "U" or (index, worktree) in (
            ("A", "A"), ("D", "D"),
        )
        code = index if index not in (" ", "?") else worktree
        rows.append({
            "file": path,
            "rel": _relative_to_project(root, target, path),
            "index": index,
            "worktree": worktree,
            "label": _LABELS.get(code, "changed"),
            "staged": index not in (" ", "?", "!"),
            "unstaged": worktree != " ",
            "untracked": untracked,
            "conflicted": conflicted,
            "origin": origin,
        })

    # In-project first, then alphabetical. The panel groups on `rel`, and a
    # stable order is what stops rows jumping around under the cursor while a
    # poll refreshes them.
    rows.sort(key=lambda r: (r["rel"] is None, (r["rel"] or r["file"]).lower()))
    return rows


async def _identity(target: Path) -> dict[str, str] | None:
    """The name and email commits here would be attributed to, if both exist.

    Reported rather than assumed because a machine with neither set fails at
    `git commit` with a wall of text about --global, at the exact moment the
    user is trying to save work. Knowing up front lets the panel offer the
    one form that fixes it.
    """
    _, name, _ = await _run(target, "config", "--get", "user.name", check=False)
    _, email, _ = await _run(target, "config", "--get", "user.email", check=False)
    name, email = name.strip(), email.strip()
    if not name or not email:
        return None
    return {"name": name, "email": email}


async def _remotes(target: Path) -> list[dict[str, str]]:
    """Configured remotes as {name, url}, one entry per name.

    ``git remote -v`` prints each twice, once for fetch and once for push.
    The panel only ever wants "where does origin point", so the duplicate is
    collapsed here rather than in three different callers.
    """
    _, out, _ = await _run(target, "remote", "-v", check=False)
    seen: dict[str, str] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            seen.setdefault(parts[0], parts[1])
    return [{"name": name, "url": url} for name, url in seen.items()]


async def _tracking(target: Path) -> tuple[str | None, int, int]:
    """(upstream ref, commits ahead of it, commits behind it).

    All three come from refs already on disk: no fetch, no network, nothing
    that can fail slowly. That is what makes it safe to include in a status
    the UI polls every few seconds.
    """
    code, upstream, _ = await _run(
        target, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}",
        check=False,
    )
    name = upstream.strip()
    if code != 0 or not name:
        return None, 0, 0
    code, counts, _ = await _run(
        target, "rev-list", "--left-right", "--count", f"{name}...HEAD", check=False,
    )
    if code != 0:
        return name, 0, 0
    parts = counts.split()
    if len(parts) != 2:
        return name, 0, 0
    behind, ahead = parts
    return name, int(ahead), int(behind)


async def _head(target: Path) -> dict[str, Any] | None:
    code, out, _ = await _run(
        target, "log", "-1", f"--format={_LOG_FORMAT}", check=False,
    )
    if code != 0 or not out.strip():
        return None
    commits = _parse_log(out)
    return commits[0] if commits else None


async def status(raw_path: str) -> dict[str, Any]:
    """Everything the Git tab needs for one paint, in one call.

    Deliberately a single round trip. The panel polls this, and a status
    assembled from five requests would show a branch from one moment beside
    a file list from another - the sort of inconsistency that reads as a bug
    in git rather than in the UI.
    """
    if git_binary() is None:
        return _offline(
            "git is not installed, or not on this machine's PATH. Install it "
            "from https://git-scm.com and reload Genesis.",
        )

    target = _project(raw_path)
    _, version, _ = await _run(target, "--version", check=False)

    root = await _root(target)
    if root is None:
        return _offline(
            f"{target.name} is not in a git repository yet. Start one to keep "
            f"a history of what Genesis changes.",
            version=version.strip() or None,
            has_gitignore=_is_file(target / ".gitignore"),
        )

    _, prefix, _ = await _run(target, "rev-parse", "--show-prefix", check=False)
    _, branch, _ = await _run(
        target, "symbolic-ref", "--quiet", "--short", "HEAD", check=False,
    )
    unborn_code, _, _ = await _run(target, "rev-parse", "--verify", "-q", "HEAD",
                                   check=False)
    unborn = unborn_code != 0

    _, raw, _ = await _run(
        target, "status", "--porcelain", "-z", "--untracked-files=all",
    )
    files = _parse_status(raw, root, target)

    remotes = await _remotes(target)
    head = None if unborn else await _head(target)
    upstream, ahead, behind = await _tracking(target)

    detached = not branch.strip() and not unborn
    short = ""
    if detached:
        _, short, _ = await _run(target, "rev-parse", "--short", "HEAD", check=False)

    return {
        "available": True,
        "version": version.strip() or None,
        "repo": True,
        "root": str(root),
        "prefix": prefix.strip(),
        "branch": branch.strip() or None,
        "detached": detached,
        "unborn": unborn,
        "head": head,
        "files": files,
        "staged": sum(1 for f in files if f["staged"]),
        "unstaged": sum(1 for f in files if f["unstaged"] and not f["untracked"]),
        "untracked": sum(1 for f in files if f["untracked"]),
        "conflicted": sum(1 for f in files if f["conflicted"]),
        "clean": not files,
        "identity": await _identity(target),
        "remotes": remotes,
        # Counted against the remote-tracking ref we already have, so this is
        # local arithmetic and costs nothing - but it is therefore only as
        # fresh as the last fetch. The UI says "since last fetch" for exactly
        # that reason; claiming it is live would be the lie.
        "upstream": upstream,
        "ahead": ahead,
        "behind": behind,
        "has_gitignore": _is_file(root / ".gitignore")
                         or _is_file(target / ".gitignore"),
        "reason": (
            f"This repository has no commits yet - {branch.strip() or 'the branch'} "
            f"is unborn until the first one."
        ) if unborn else (
            f"HEAD is detached at {short.strip()}. Commits here belong to no "
            f"branch; check one out before you rely on them."
        ) if detached else None,
    }


# ---------------------------------------------------------------------------
# Starting a repository
# ---------------------------------------------------------------------------

def write_gitignore(target: Path, *, force: bool = False) -> bool:
    """Write the standard .gitignore, unless the project already has one.

    Never overwrites by default: an existing .gitignore is a decision
    somebody made, and silently replacing it is how a project starts
    committing its own .env.
    """
    dest = target / ".gitignore"
    if dest.exists() and not force:
        return False
    dest.write_text(GITIGNORE, encoding="utf-8")
    return True


async def init_repo(
    raw_path: str, *, initial_commit: bool = True, gitignore: bool = True,
) -> dict[str, Any]:
    """``git init`` in the project, plus the two things nobody wants to do by
    hand afterwards: a .gitignore, and a first commit to diff against.

    Refuses when the folder is already inside a repository. Nesting one repo
    inside another is almost always an accident, and the accident is silent -
    the inner repo simply shadows the outer one for everything underneath it.
    """
    if git_binary() is None:
        raise GitError(
            "git is not installed, or not on this machine's PATH. Install it "
            "from https://git-scm.com and reload Genesis.",
        )

    target = _project(raw_path)
    existing = await _root(target)
    if existing is not None:
        raise GitError(
            f"{target.name} is already inside the git repository at "
            f"{existing}. Nesting a second repository under it would hide "
            f"everything here from the one you have.",
        )

    await _run(target, "init", "-q")

    # git's own default is `master` plus a paragraph of hint text on every
    # init. If the user has expressed a preference we leave it alone; if they
    # have not, retarget the (still unborn, so cost-free) branch to `main`
    # rather than pick a fight with the hint.
    code, configured, _ = await _run(
        target, "config", "--get", "init.defaultBranch", check=False,
    )
    if code != 0 or not configured.strip():
        _, current, _ = await _run(
            target, "symbolic-ref", "--quiet", "--short", "HEAD", check=False,
        )
        if current.strip() == "master":
            await _run(target, "symbolic-ref", "HEAD", "refs/heads/main", check=False)

    if gitignore:
        write_gitignore(target)

    if initial_commit:
        identity = await _identity(target)
        if identity is None:
            # Not fatal: the repo exists, the .gitignore is there, and the
            # panel can now ask for a name and email and offer the commit
            # again. Failing the whole init over it would leave nothing.
            result = await status(raw_path)
            result["note"] = (
                "Repository started. git has no user.name / user.email on this "
                "machine, so there is no one to attribute a commit to yet - set "
                "them and commit when you are ready."
            )
            return result
        await _run(target, "add", "-A", "--", ".")
        # --allow-empty so an init in a folder where everything is ignored
        # still produces the root commit everything else diffs against.
        await _run(
            target, "commit", "-q", "--allow-empty",
            "-m", "Initial commit - scaffolded by Cosmonapse Genesis",
        )

    result = await status(raw_path)
    result["note"] = f"Started a git repository in {target.name}."
    return result


async def set_identity(raw_path: str, name: str, email: str) -> dict[str, Any]:
    """Set ``user.name`` / ``user.email`` for this repository only.

    Repository-scoped on purpose. Genesis is not in a position to know
    whether the identity someone types into a panel is the one they want on
    every repo on the machine, and ``--global`` is not something a UI should
    reach for on their behalf.
    """
    target = _project(raw_path)
    name, email = (name or "").strip(), (email or "").strip()
    if not name or not email:
        raise ValueError("name and email are both required")
    if await _root(target) is None:
        raise GitError(f"{target.name} is not in a git repository.")
    await _run(target, "config", "user.name", name)
    await _run(target, "config", "user.email", email)
    return await status(raw_path)


# ---------------------------------------------------------------------------
# Staging and committing
# ---------------------------------------------------------------------------

async def stage(raw_path: str, files: list[str], *, staged: bool) -> dict[str, Any]:
    """Add the given paths to the index, or take them back out of it."""
    target = _project(raw_path)
    root = await _root(target)
    if root is None:
        raise GitError(f"{target.name} is not in a git repository.")
    paths = [_in_repo(root, f) for f in files if (f or "").strip()]
    if not paths:
        raise ValueError("at least one file is required")

    if staged:
        # `--` before the paths, always: a file called `-f` is legal and
        # would otherwise be read as a flag.
        await _run(root, "add", "--", *paths)
    else:
        code, _, err = await _run(
            root, "restore", "--staged", "--", *paths, check=False,
        )
        if code != 0:
            # `git restore` needs a HEAD to restore the index from, so on an
            # unborn branch it fails and the equivalent is dropping the entry
            # from the index entirely. `--cached` keeps the file on disk.
            code, _, err2 = await _run(
                root, "rm", "-q", "--cached", "-r", "--", *paths, check=False,
            )
            if code != 0:
                raise GitError(err2.strip() or err.strip() or "Couldn't unstage that.",
                               code=code, stderr=err2 or err)
    return await status(raw_path)


async def commit(
    raw_path: str, message: str, *, stage_all: bool = False,
) -> dict[str, Any]:
    """Commit the index, optionally staging everything in the repo first.

    Standard git semantics, on purpose: what gets committed is what is in the
    index, exactly as it would be from a terminal. ``stage_all`` is the
    one-click checkpoint the Git tab offers, and it is spelled out in the UI
    as "stage everything, then commit" rather than hidden inside the button.
    """
    target = _project(raw_path)
    root = await _root(target)
    if root is None:
        raise GitError(f"{target.name} is not in a git repository.")

    message = (message or "").strip()
    if not message:
        raise ValueError("a commit message is required")

    if await _identity(target) is None:
        raise GitError(
            "git has no user.name / user.email set, so there is nobody to "
            "attribute this commit to. Set them for this repository and try "
            "again.",
        )

    if stage_all:
        await _run(root, "add", "-A", "--", ".")

    code, _, _ = await _run(root, "diff", "--cached", "--quiet", check=False)
    if code == 0:
        raise GitError(
            "Nothing is staged, so there is nothing to commit. Stage the "
            "files you want in this commit first.",
        )

    # The message arrives as one argument to -m. It is never interpolated
    # into a shell, so a message containing quotes, newlines or $(…) is just
    # a message.
    await _run(root, "commit", "-q", "-m", message)

    result = await status(raw_path)
    result["committed"] = result["head"]
    return result


# ---------------------------------------------------------------------------
# Reading history
# ---------------------------------------------------------------------------

def _parse_log(payload: str) -> list[dict[str, Any]]:
    commits: list[dict[str, Any]] = []
    for record in payload.split(_RS):
        line = record.strip("\n")
        if not line.strip():
            continue
        parts = line.split(_US)
        if len(parts) < 7:
            continue
        sha, short, author, email, date, subject, parents = parts[:7]
        commits.append({
            "sha": sha,
            "short": short,
            "author": author,
            "email": email,
            "date": date,
            "subject": subject,
            "parents": [p for p in parents.split(" ") if p],
            # A root commit has no parent, which is why its diff has to be
            # taken against the empty tree rather than HEAD~1. The UI shows
            # it differently too - there is no "before" to compare with.
            "root": not parents.strip(),
        })
    return commits


async def log(
    raw_path: str, *, limit: int = DEFAULT_LOG_LIMIT, file: str = "",
) -> dict[str, Any]:
    """The commit list, newest first, optionally narrowed to one path."""
    target = _project(raw_path)
    root = await _root(target)
    if root is None:
        raise GitError(f"{target.name} is not in a git repository.")

    limit = max(1, min(int(limit or DEFAULT_LOG_LIMIT), 500))
    args = ["log", f"--max-count={limit}", f"--format={_LOG_FORMAT}"]
    if file:
        # `--follow` so renaming a component does not truncate its history at
        # the rename, which is exactly the moment a per-file log is useful.
        args += ["--follow", "--", _in_repo(root, file)]

    code, out, err = await _run(root, *args, check=False)
    if code != 0:
        # An unborn branch is not an error worth raising - it is the honest
        # answer "no commits yet", and the panel says so.
        if "does not have any commits yet" in err or "unknown revision" in err:
            return {"commits": [], "limit": limit, "file": file or None}
        raise GitError(err.strip() or "Couldn't read the log.", code=code, stderr=err)

    return {"commits": _parse_log(out), "limit": limit, "file": file or None}


def _parse_numstat(payload: str) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for line in payload.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added, removed, path = parts[0], parts[1], parts[-1]
        changes.append({
            "file": path,
            # "-" where git means binary: there is no line count to give.
            "added": None if added == "-" else int(added),
            "removed": None if removed == "-" else int(removed),
            "binary": added == "-",
        })
    return changes


async def show(raw_path: str, sha: str) -> dict[str, Any]:
    """One commit: its header, what it touched, and the diff.

    Split from ``log`` rather than folded into it because the list is polled
    and this is not. Carrying every commit's diff in the list would make
    scrolling history cost the same as reading all of it.
    """
    target = _project(raw_path)
    root = await _root(target)
    if root is None:
        raise GitError(f"{target.name} is not in a git repository.")

    rev = (sha or "").strip()
    if not rev:
        raise ValueError("sha is required")
    # Refuse anything that isn't a plain revision name before it reaches git:
    # `..`, `^{}` and friends are legal revisions that mean something quite
    # different from "this commit", and the panel only ever means the latter.
    if not rev.replace("-", "").replace("_", "").replace("/", "").isalnum():
        raise ValueError(f"{sha!r} is not a commit id")

    code, header, err = await _run(
        root, "show", "-s", f"--format={_LOG_FORMAT}", rev, check=False,
    )
    if code != 0:
        raise GitError(err.strip() or f"No commit {rev} in this repository.",
                       code=code, stderr=err)
    commits = _parse_log(header)
    if not commits:
        raise GitError(f"No commit {rev} in this repository.")

    _, stat, _ = await _run(root, "show", "--numstat", "--format=", rev, check=False)
    _, body, _ = await _run(
        root, "show", "--format=", "--unified=3", rev, check=False,
    )
    truncated = len(body) > _MAX_DIFF_BYTES
    return {
        "commit": commits[0],
        "changes": _parse_numstat(stat),
        "diff": body[:_MAX_DIFF_BYTES],
        "truncated": truncated,
    }


# ---------------------------------------------------------------------------
# Diffs
# ---------------------------------------------------------------------------

def _looks_binary(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return b"\0" in fh.read(8000)
    except OSError:
        return False


def _new_file_diff(root: Path, rel: str) -> dict[str, Any]:
    """A unified diff for a file git has never seen.

    ``git diff`` has nothing to say about an untracked path, and
    ``--no-index`` against /dev/null is a platform gamble not worth taking on
    a tool that runs on Windows. Reading the file and marking every line
    added produces the same thing the user would see after staging it, with
    no special case anywhere downstream.
    """
    full = root / rel
    if _looks_binary(full):
        return {"diff": "", "binary": True, "truncated": False, "empty": False}
    try:
        text = full.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise GitError(f"Couldn't read {rel}: {e}")

    truncated = len(text) > _MAX_NEW_FILE_BYTES
    text = text[:_MAX_NEW_FILE_BYTES]
    lines = text.splitlines()
    header = (
        f"diff --git a/{rel} b/{rel}\n"
        f"new file\n--- /dev/null\n+++ b/{rel}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n"
    )
    return {
        "diff": header + "".join("+" + line + "\n" for line in lines),
        "binary": False,
        "truncated": truncated,
        "empty": not lines,
    }


async def diff(
    raw_path: str, file: str, *, staged: bool = False, sha: str = "",
) -> dict[str, Any]:
    """The change to one file: worktree, index, or as it landed in a commit."""
    target = _project(raw_path)
    root = await _root(target)
    if root is None:
        raise GitError(f"{target.name} is not in a git repository.")
    rel = _in_repo(root, file)

    if sha:
        detail = await show(raw_path, sha)
        # Re-run scoped to the path rather than filtering the whole-commit
        # diff client-side: git already knows how to do this, and a text
        # split on "diff --git" is wrong the moment a filename contains one.
        _, body, _ = await _run(
            root, "show", "--format=", "--unified=3", sha, "--", rel, check=False,
        )
        return {
            "file": rel,
            "rel": _relative_to_project(root, target, rel),
            "diff": body[:_MAX_DIFF_BYTES],
            "binary": "Binary files" in body,
            "truncated": len(body) > _MAX_DIFF_BYTES,
            "empty": not body.strip(),
            "sha": detail["commit"]["short"],
            "staged": False,
        }

    # An untracked file is invisible to `git diff` in either direction, so
    # it is answered before asking git anything.
    _, raw, _ = await _run(
        root, "status", "--porcelain", "-z", "--untracked-files=all", "--", rel,
    )
    rows = _parse_status(raw, root, target)
    if rows and rows[0]["untracked"]:
        synth = _new_file_diff(root, rel)
        return {
            "file": rel,
            "rel": _relative_to_project(root, target, rel),
            "sha": None,
            "staged": False,
            **synth,
        }

    args = ["diff", "--unified=3"]
    if staged:
        args.append("--cached")
    args += ["--", rel]
    _, body, _ = await _run(root, *args, check=False)

    return {
        "file": rel,
        "rel": _relative_to_project(root, target, rel),
        "diff": body[:_MAX_DIFF_BYTES],
        "binary": "Binary files" in body,
        "truncated": len(body) > _MAX_DIFF_BYTES,
        "empty": not body.strip(),
        "sha": None,
        "staged": staged,
    }


# ---------------------------------------------------------------------------
# Undo
# ---------------------------------------------------------------------------

async def restore(raw_path: str, file: str, *, sha: str = "") -> dict[str, Any]:
    """Put one file back - to its last commit, or to a chosen one.

    Always exactly one path. This is the destructive end of the module: it
    overwrites work that has not been committed, and the guarantee that
    makes it safe to offer from a button is that it can only ever overwrite
    the file whose name is on the button. A bare ``git checkout .`` would
    quietly take everything else with it.

    A file git does not know about is refused rather than deleted. "Restore"
    on something with no committed version has no meaning that isn't
    "delete", and deleting a file the user has never saved anywhere is not a
    thing a UI should infer.
    """
    target = _project(raw_path)
    root = await _root(target)
    if root is None:
        raise GitError(f"{target.name} is not in a git repository.")
    rel = _in_repo(root, file)
    rev = (sha or "").strip() or "HEAD"
    if rev != "HEAD" and not rev.replace("-", "").replace("_", "").replace("/", "").isalnum():
        raise ValueError(f"{sha!r} is not a commit id")

    code, _, err = await _run(root, "cat-file", "-e", f"{rev}:{rel}", check=False)
    if code != 0:
        where = "the last commit" if rev == "HEAD" else rev
        raise GitError(
            f"{rel} does not exist in {where}, so there is no version of it "
            f"to restore. If it is new, delete it yourself or leave it - "
            f"Genesis will not remove a file git has never seen.",
            code=code, stderr=err,
        )

    code, _, err = await _run(root, "checkout", rev, "--", rel, check=False)
    if code != 0:
        raise GitError(err.strip() or f"Couldn't restore {rel}.",
                       code=code, stderr=err)

    result = await status(raw_path)
    result["restored"] = {"file": rel, "from": rev}
    return result


# ---------------------------------------------------------------------------
# Branches
# ---------------------------------------------------------------------------

async def branches(raw_path: str) -> dict[str, Any]:
    """Every local branch, and which one is checked out.

    Local only. Listing remote-tracking branches would invite clicking one,
    and checking out a remote-tracking ref detaches HEAD - a state that is
    perfectly normal in a terminal and completely baffling in a dropdown.
    """
    target = _project(raw_path)
    root = await _root(target)
    if root is None:
        raise GitError(f"{target.name} is not in a git repository.")

    fmt = _US.join([
        "%(refname:short)", "%(upstream:short)", "%(objectname:short)",
        "%(committerdate:iso-strict)", "%(contents:subject)",
    ])
    _, out, _ = await _run(
        root, "for-each-ref", "refs/heads", f"--format={fmt}",
        "--sort=-committerdate", check=False,
    )
    _, current, _ = await _run(
        root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False,
    )

    found: list[dict[str, Any]] = []
    for line in out.splitlines():
        parts = line.split(_US)
        if len(parts) < 5:
            continue
        name, upstream, sha, date, subject = parts[:5]
        found.append({
            "name": name,
            "upstream": upstream or None,
            "short": sha,
            "date": date,
            "subject": subject,
            "current": name == current.strip(),
        })
    return {
        "branches": found,
        "current": current.strip() or None,
        "remotes": await _remotes(target),
    }


async def _valid_branch(root: Path, name: str) -> str:
    """Let git decide what a branch may be called, rather than guessing.

    ``check-ref-format`` knows the whole rule set - no ``..``, no trailing
    ``.lock``, no control characters, no leading dash - and it will not
    drift from whatever the installed git actually accepts.
    """
    clean = (name or "").strip()
    if not clean:
        raise ValueError("a branch name is required")
    code, _, _ = await _run(root, "check-ref-format", "--branch", clean, check=False)
    if code != 0:
        raise ValueError(
            f"{clean!r} is not a name git will accept for a branch. Letters, "
            f"digits, dashes and slashes are safe.",
        )
    return clean


async def switch_branch(
    raw_path: str, name: str, *, create: bool = False,
) -> dict[str, Any]:
    """Check out a branch, or make one from where you are.

    The two halves have deliberately different rules about a dirty tree.
    Creating carries your uncommitted work onto the new branch, which is the
    single most useful thing this button does - "I started typing, and now I
    want it somewhere of its own". Switching to an *existing* branch with
    uncommitted work is refused, because whether git carries the changes over
    or refuses depends on whether they happen to touch files that differ
    between the two branches, and no one should have to predict that from a
    dropdown.
    """
    target = _project(raw_path)
    root = await _root(target)
    if root is None:
        raise GitError(f"{target.name} is not in a git repository.")
    clean = await _valid_branch(root, name)

    if not create:
        state = await status(raw_path)
        if not state["clean"]:
            raise GitError(
                f"There are uncommitted changes, so switching to {clean} could "
                f"carry them across or refuse depending on what they touch. "
                f"Commit them, or discard them, first - or create a new branch "
                f"here instead, which takes them with you on purpose.",
            )

    args = ["switch", "-c", clean] if create else ["switch", clean]
    code, _, err = await _run(root, *args, check=False)
    if code != 0 and "is not a git command" in err:
        # `git switch` arrived in 2.23. Anything older still has checkout,
        # and this is the one place the difference shows.
        args = ["checkout", "-b", clean] if create else ["checkout", clean]
        code, _, err = await _run(root, *args, check=False)
    if code != 0:
        raise GitError(err.strip() or f"Couldn't switch to {clean}.",
                       code=code, stderr=err)

    result = await status(raw_path)
    result["note"] = (
        f"Created {clean} and switched to it." if create
        else f"Switched to {clean}."
    )
    return result


# ---------------------------------------------------------------------------
# Remotes
# ---------------------------------------------------------------------------

def _check_remote_url(url: str) -> str:
    """Accept the URL forms git actually clones from, and nothing else.

    ``ext::``, ``--upload-pack=`` and friends are how a hostile clone URL
    turns into command execution. Genesis' URLs come from a form and from a
    repo list, so an allow-list costs nothing and closes that off before the
    string is ever an argv element.
    """
    clean = (url or "").strip()
    if not clean:
        raise ValueError("a repository URL is required")
    if clean.startswith("-"):
        raise ValueError("a repository URL cannot start with a dash")
    lowered = clean.lower()
    # file:// is on the list deliberately. A bare repo on a disk or a network
    # share is a real thing small teams use, it carries none of the risk the
    # rejected forms do, and it is what makes clone/push/pull testable without
    # a network - which is worth a great deal on its own.
    if lowered.startswith(("https://", "http://", "ssh://", "git://", "file://")):
        return clean
    # scp-style: git@github.com:owner/repo.git
    if "@" in clean and ":" in clean.split("@", 1)[1] and "://" not in clean:
        return clean
    raise ValueError(
        f"{clean!r} is not a repository URL Genesis will use. Give an https:// "
        f"address, or the git@host:owner/repo form.",
    )


async def set_remote(raw_path: str, url: str, name: str = "origin") -> dict[str, Any]:
    """Point a repository at a remote, adding or replacing it.

    The "I have a local repo and want it on GitHub" path. Adding the remote
    is all this does - the first push is still a separate, deliberate press,
    so nothing leaves the machine as a side effect of filling in a URL.
    """
    target = _project(raw_path)
    root = await _root(target)
    if root is None:
        raise GitError(f"{target.name} is not in a git repository.")
    clean_url = _check_remote_url(url)
    clean_name = await _valid_branch(root, name or "origin")

    existing = {r["name"] for r in await _remotes(target)}
    verb = ["set-url"] if clean_name in existing else ["add"]
    code, _, err = await _run(
        root, "remote", *verb, clean_name, clean_url, check=False,
    )
    if code != 0:
        raise GitError(err.strip() or f"Couldn't set the remote {clean_name}.",
                       code=code, stderr=err)

    result = await status(raw_path)
    result["note"] = f"{clean_name} now points at {clean_url}."
    return result


# ---------------------------------------------------------------------------
# The network
# ---------------------------------------------------------------------------

async def clone(parent: str, url: str, name: str = "") -> dict[str, Any]:
    """Clone into ``parent/name``, refusing to land on top of anything.

    Never clones into the selected folder itself, always into a new child of
    it. A clone that unpacks a repository over whatever the user happened to
    have selected is the kind of mistake there is no undo for, and the cost
    of avoiding it is one directory level.
    """
    if git_binary() is None:
        raise GitError(
            "git is not installed, or not on this machine's PATH. Install it "
            "from https://git-scm.com and reload Genesis.",
        )
    clean_url = _check_remote_url(url)

    parent_dir = _expanded(parent)
    if not _is_dir(parent_dir):
        raise FileNotFoundError(f"{parent_dir} is not a directory")

    folder = (name or "").strip() or _repo_name(clean_url)
    if not folder or folder in (".", "..") or "/" in folder or "\\" in folder:
        raise ValueError(f"{folder!r} is not a usable folder name")

    dest = parent_dir / folder
    if _is_dir(dest) and _has_contents(dest):
        raise GitError(
            f"{dest} already exists and is not empty. Pick another folder, or "
            f"open what is already there.",
        )

    code, _, err = await _run_net(
        parent_dir, "clone", "--", clean_url, folder, check=False,
    )
    if code != 0:
        raise GitError(
            err.strip() or f"Couldn't clone {clean_url}.", code=code, stderr=err,
        )

    result = await status(str(dest))
    result["cloned_to"] = str(dest)
    result["note"] = f"Cloned {clean_url} into {folder}."
    return result


def _repo_name(url: str) -> str:
    """``https://host/owner/repo.git`` -> ``repo``."""
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    if ":" in tail and "/" not in tail:      # git@host:repo.git
        tail = tail.rsplit(":", 1)[-1]
    return tail.removesuffix(".git")


async def _current_branch(root: Path) -> str:
    _, name, _ = await _run(
        root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False,
    )
    branch = name.strip()
    if not branch:
        raise GitError(
            "HEAD is detached, so there is no branch to push or pull. Check "
            "one out first.",
        )
    return branch


async def _remote_for(target: Path, remote: str) -> str:
    known = await _remotes(target)
    if not known:
        raise GitError(
            "This repository has no remote, so there is nowhere to push to. "
            "Add one first - the Publish button does it in one step.",
        )
    names = {r["name"] for r in known}
    clean = (remote or "").strip() or ("origin" if "origin" in names else
                                       sorted(names)[0])
    if clean not in names:
        raise GitError(f"There is no remote called {clean!r} in this repository.")
    return clean


async def push(
    raw_path: str, *, remote: str = "", branch: str = "",
) -> dict[str, Any]:
    """Publish the current branch, setting its upstream the first time.

    ``-u`` only when there isn't an upstream already, so a repeat push never
    silently repoints a branch someone deliberately tracked elsewhere.
    """
    target = _project(raw_path)
    root = await _root(target)
    if root is None:
        raise GitError(f"{target.name} is not in a git repository.")

    clean_remote = await _remote_for(target, remote)
    clean_branch = (branch or "").strip() or await _current_branch(root)
    upstream, _, _ = await _tracking(target)

    args = ["push"] + ([] if upstream else ["-u"]) + [clean_remote, clean_branch]
    code, _, err = await _run_net(root, *args, check=False)
    if code != 0:
        detail = err.strip()
        if "non-fast-forward" in detail or "fetch first" in detail or "rejected" in detail:
            raise GitError(
                f"{clean_remote} has commits your branch does not. Your commit "
                f"is safe here - pull first, then push again. If pull refuses "
                f"too, the two have genuinely diverged and that needs a merge "
                f"in a terminal.",
                code=code, stderr=err,
            )
        if "could not read" in detail.lower() or "authentication" in detail.lower():
            raise GitError(
                f"{clean_remote} refused the credentials. Reconnect the account "
                f"on the start screen, or check the token has not expired.",
                code=code, stderr=err,
            )
        raise GitError(detail or f"Couldn't push to {clean_remote}.",
                       code=code, stderr=err)

    result = await status(raw_path)
    result["note"] = f"Pushed {clean_branch} to {clean_remote}."
    return result


async def pull(raw_path: str, *, remote: str = "", branch: str = "") -> dict[str, Any]:
    """Fast-forward onto the remote, or say why it can't.

    ``--ff-only`` is the whole policy. Merge resolution is out of scope for
    this panel (see the module docstring), and a pull that quietly created a
    merge commit would be the one action here whose result you could not read
    off the screen that triggered it.
    """
    target = _project(raw_path)
    root = await _root(target)
    if root is None:
        raise GitError(f"{target.name} is not in a git repository.")

    clean_remote = await _remote_for(target, remote)
    clean_branch = (branch or "").strip() or await _current_branch(root)

    code, _, err = await _run_net(
        root, "pull", "--ff-only", clean_remote, clean_branch, check=False,
    )
    if code != 0:
        detail = err.strip()
        if "not possible to fast-forward" in detail or "diverging" in detail:
            raise GitError(
                f"Your branch and {clean_remote}/{clean_branch} have both moved "
                f"on, so this cannot be a fast-forward. That needs a merge or a "
                f"rebase, which Genesis deliberately leaves to a terminal.",
                code=code, stderr=err,
            )
        raise GitError(detail or f"Couldn't pull from {clean_remote}.",
                       code=code, stderr=err)

    result = await status(raw_path)
    result["note"] = f"Pulled {clean_branch} from {clean_remote}."
    return result
