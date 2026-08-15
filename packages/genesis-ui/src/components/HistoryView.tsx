import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties } from "react";
import {
  gitBranch,
  gitBranches,
  gitCommit,
  gitDiff,
  gitIdentity,
  gitInit,
  gitLog,
  gitPull,
  gitPush,
  gitRemote,
  gitRestore,
  gitShow,
  gitStage,
  gitStatus,
} from "../api";
import { C, MONO } from "../theme";
import type {
  GitBranch,
  GitCommit,
  GitCommitDetail,
  GitDiff,
  GitFile,
  GitStatus,
  InitError,
  ScaffoldResult,
} from "../types";

/** How often the working tree is re-read while this tab is in front. */
const POLL_MS = 5000;

/**
 * The History tab: what has changed, and what it changed from.
 *
 * Genesis edits real source files, and most of what it does is structural -
 * adding a component writes a module *and* rewrites brain.py, and every Code
 * tab edit replaces a whole declaration in place. `_archive/` catches exactly
 * one of those. This tab is the undo for the rest, and it is deliberately the
 * user's own git rather than a private history: a repository Genesis touched
 * has to stay one they can drive from a terminal.
 *
 * Two halves, same split as the Test tab:
 *
 *   Changes   what the working tree looks like right now, one row per file,
 *             with the diff beside it and a commit box under it.
 *   History   what is already committed, one row per commit, with the whole
 *             commit's diff beside it.
 *
 * Nothing here commits on its own. An edit leaves the tree dirty and says so;
 * turning that into a commit stays something you decide to do. A history
 * written behind your back is one you cannot read, and it would race a `git
 * commit` you ran in a terminal on the same repo.
 */
export function HistoryView({
  scaffold,
  onChanged,
}: {
  scaffold: ScaffoldResult;
  onChanged: () => void;
}) {
  const path = scaffold.path;
  const [status, setStatus] = useState<GitStatus | null>(null);
  const [tab, setTab] = useState<"changes" | "history">("changes");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [publishing, setPublishing] = useState(false);

  const refresh = useCallback(() => {
    gitStatus(path)
      .then(setStatus)
      .catch((e) => setError((e as InitError).error || "Couldn't read the repository."));
  }, [path]);

  useEffect(refresh, [refresh]);

  // Polled, because the tree is shared. A `git add` in a terminal, a build
  // that touched a file, another Genesis tab on the same project - all of it
  // is invisible to this one unless it looks again.
  useEffect(() => {
    if (!status?.repo) return;
    const t = setInterval(refresh, POLL_MS);
    return () => clearInterval(t);
  }, [status?.repo, refresh]);

  /** Run one mutating call, keep its status, and surface its refusal. */
  async function run(fn: () => Promise<GitStatus>) {
    setBusy(true);
    setError(null);
    try {
      setStatus(await fn());
    } catch (e) {
      setError((e as InitError).error || "git wouldn't do that.");
    } finally {
      setBusy(false);
    }
  }

  if (!status) {
    return (
      <div style={{ padding: 24, fontFamily: MONO, fontSize: 14.5, color: C.textFaint }}>
        {error ?? "Reading the repository…"}
      </div>
    );
  }

  if (!status.available || !status.repo) {
    return <Setup status={status} busy={busy} error={error} onInit={(o) => run(() => gitInit({ path, ...o }))} />;
  }

  return (
    <div style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" }}>
      <RepoBar
        path={path}
        status={status}
        tab={tab}
        busy={busy}
        error={error}
        onTab={setTab}
        onRefresh={refresh}
        onSwitch={(name, create) => run(() => gitBranch(path, name, create))}
        onPush={() => run(() => gitPush(path))}
        onPull={async () => {
          await run(() => gitPull(path));
          // A pull rewrites files on disk - very often brain.py - so the
          // canvas and the Code tab are stale until the project is re-read.
          onChanged();
        }}
        onPublish={() => setPublishing((v) => !v)}
        publishing={publishing}
      />

      {publishing && (
        <PublishForm
          busy={busy}
          onCancel={() => setPublishing(false)}
          onSave={async (url) => {
            await run(() => gitRemote(path, url));
            setPublishing(false);
          }}
        />
      )}

      {status.identity === null && (
        <IdentityForm
          busy={busy}
          onSave={(name, email) => run(() => gitIdentity(path, name, email))}
        />
      )}

      {status.reason && <Banner text={status.reason} />}

      {tab === "changes" ? (
        <Changes
          path={path}
          status={status}
          busy={busy}
          onStage={(files, staged) => run(() => gitStage(path, files, staged))}
          onCommit={(message, stageAll) =>
            run(() => gitCommit({ path, message, stage_all: stageAll }))
          }
          onRestore={async (file, sha) => {
            await run(() => gitRestore(path, file, sha));
            // A restore rewrites a file on disk, and that file may well be
            // brain.py - so the canvas and the Code tab are now looking at
            // something that no longer exists. Re-read the project too.
            onChanged();
          }}
        />
      ) : (
        <History path={path} />
      )}
    </div>
  );
}

/* ────────────────────────  no repository yet  ──────────────────────── */

/**
 * The one screen that has to be useful when there is nothing to show.
 *
 * "Not in a repository" and "no git on this machine" are answers rather than
 * errors, and each has exactly one next step. Making either of them an error
 * state would leave the tab unable to offer the thing it exists for.
 */
function Setup({
  status,
  busy,
  error,
  onInit,
}: {
  status: GitStatus;
  busy: boolean;
  error: string | null;
  onInit: (opts: { initial_commit: boolean; gitignore: boolean }) => void;
}) {
  const [commit, setCommit] = useState(true);
  const [gitignore, setGitignore] = useState(true);

  return (
    <div style={{ flex: 1, overflowY: "auto", padding: 28 }}>
      <div style={{ ...cardStyle, maxWidth: 620 }}>
        <div style={{ fontFamily: MONO, fontSize: 16, color: C.text, marginBottom: 10 }}>
          {status.available ? "No repository here yet" : "git isn't installed"}
        </div>
        <p style={{ color: C.textDim, fontSize: 14.5, lineHeight: 1.6, margin: "0 0 16px" }}>
          {status.reason}
        </p>

        {status.available ? (
          <>
            <p style={{ color: C.textFaint, fontSize: 13.5, lineHeight: 1.6, margin: "0 0 16px" }}>
              Genesis rewrites brain.py every time you add or remove a component, and
              every Code-tab edit replaces a declaration in place. A repository is what
              makes those reversible - and it is a normal one, so everything you do
              here is visible to git in a terminal.
            </p>
            <Check checked={gitignore} onChange={setGitignore} label="Write a .gitignore" hint="caches, virtualenvs, .env, _archive/" />
            <Check checked={commit} onChange={setCommit} label="Make the first commit" hint="something for later changes to diff against" />
            <button
              disabled={busy}
              onClick={() => onInit({ initial_commit: commit, gitignore })}
              style={primaryStyle(!busy)}
            >
              {busy ? "Starting…" : "Start a repository"}
            </button>
          </>
        ) : (
          <a href="https://git-scm.com" target="_blank" rel="noreferrer" style={{ color: C.accent2, fontFamily: MONO, fontSize: 14 }}>
            git-scm.com
          </a>
        )}

        {error && <div style={{ ...errorStyle, marginTop: 14 }}>{error}</div>}
      </div>
    </div>
  );
}

function Check({
  checked,
  onChange,
  label,
  hint,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
  hint: string;
}) {
  return (
    <label style={{ display: "flex", alignItems: "baseline", gap: 9, marginBottom: 10, cursor: "pointer" }}>
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} style={{ accentColor: C.accent2 }} />
      <span style={{ fontFamily: MONO, fontSize: 14, color: C.text }}>{label}</span>
      <span style={{ fontSize: 13, color: C.textFaint }}>{hint}</span>
    </label>
  );
}

/* ────────────────────────────  the bar  ────────────────────────────── */

function RepoBar({
  path,
  status,
  tab,
  busy,
  error,
  onTab,
  onRefresh,
  onSwitch,
  onPush,
  onPull,
  onPublish,
  publishing,
}: {
  path: string;
  status: GitStatus;
  tab: "changes" | "history";
  busy: boolean;
  error: string | null;
  onTab: (t: "changes" | "history") => void;
  onRefresh: () => void;
  onSwitch: (name: string, create: boolean) => void;
  onPush: () => void;
  onPull: () => void;
  onPublish: () => void;
  publishing: boolean;
}) {
  const dirty = !status.clean;
  const hasRemote = status.remotes.length > 0;
  return (
    <div
      style={{
        flexShrink: 0,
        display: "flex",
        alignItems: "center",
        gap: 12,
        padding: "11px 20px",
        borderBottom: `1px solid ${C.border}`,
        background: "var(--bg-rail)",
      }}
    >
      <div style={{ display: "flex", gap: 6 }}>
        {(["changes", "history"] as const).map((t) => {
          const on = t === tab;
          return (
            <div
              key={t}
              onClick={() => onTab(t)}
              style={{
                padding: "4px 14px",
                borderRadius: 8,
                cursor: "pointer",
                fontFamily: MONO,
                fontSize: 14,
                textTransform: "capitalize",
                color: on ? C.accent2 : C.textDim,
                background: on ? "rgba(var(--accent2-rgb), 0.12)" : "transparent",
                border: "1px solid " + (on ? "rgba(var(--accent2-rgb), 0.4)" : C.border),
              }}
            >
              {t}
            </div>
          );
        })}
      </div>

      <span style={{ color: C.textFaint, fontWeight: 600 }}>│</span>

      <BranchMenu path={path} status={status} busy={busy} onSwitch={onSwitch} />

      {/* Ahead/behind is counted against the tracking ref already on disk, so
          it is free - and therefore only as fresh as the last fetch. Pulling
          is what updates it, which the tooltip says rather than leaving the
          number to imply something it cannot know. */}
      {status.upstream && (status.ahead > 0 || status.behind > 0) && (
        <span
          style={{ fontFamily: MONO, fontSize: 13.5, color: C.textDim }}
          title={`Against ${status.upstream}, as of the last fetch or pull.`}
        >
          {status.ahead > 0 && <span style={{ color: C.ok }}>{status.ahead} ahead</span>}
          {status.ahead > 0 && status.behind > 0 && " · "}
          {status.behind > 0 && <span style={{ color: C.warn }}>{status.behind} behind</span>}
        </span>
      )}

      {status.head && (
        <span
          style={{ fontFamily: MONO, fontSize: 13.5, color: C.textDim, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: 340 }}
          title={`${status.head.sha} · ${status.head.author} · ${fmtDate(status.head.date)}`}
        >
          {status.head.short} {status.head.subject}
        </span>
      )}

      <span style={{ fontSize: 13.5, color: dirty ? C.warn : C.ok, fontFamily: MONO }}>
        {dirty
          ? [
              status.staged ? `${status.staged} staged` : "",
              status.unstaged ? `${status.unstaged} changed` : "",
              status.untracked ? `${status.untracked} new` : "",
              status.conflicted ? `${status.conflicted} conflicted` : "",
            ]
              .filter(Boolean)
              .join(" · ")
          : "clean"}
      </span>

      {error && <span style={{ fontSize: 13.5, color: C.accent3, maxWidth: 520 }}>{error}</span>}

      <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 8 }}>
        {hasRemote ? (
          <>
            <button
              onClick={onPull}
              disabled={busy}
              title={`Fast-forward onto ${status.upstream ?? "the remote"}. Refuses rather than merging if both sides have moved.`}
              style={ghostStyle}
            >
              Pull{status.behind > 0 ? ` (${status.behind})` : ""}
            </button>
            <button
              onClick={onPush}
              disabled={busy}
              title={
                status.upstream
                  ? `Push this branch to ${status.upstream}.`
                  : "Push this branch and set it to track the remote."
              }
              style={ghostStyle}
            >
              Push{status.ahead > 0 ? ` (${status.ahead})` : ""}
            </button>
          </>
        ) : (
          <button
            onClick={onPublish}
            disabled={busy}
            title="This repository has no remote yet. Point it at one so it can be pushed."
            style={{ ...ghostStyle, color: publishing ? C.accent2 : "var(--text-dim)" }}
          >
            Publish…
          </button>
        )}
        <button onClick={onRefresh} disabled={busy} style={ghostStyle}>
          Refresh
        </button>
      </div>
    </div>
  );
}

/**
 * The branch pill, with the two things you actually do to a branch behind it.
 *
 * Local branches only. A remote-tracking ref is checkoutable and doing so
 * detaches HEAD - a state that is ordinary in a terminal and completely
 * baffling arrived at from a dropdown, so it simply is not offered.
 *
 * The list is fetched when the menu opens rather than polled with the status:
 * branches change when you change them, and adding a second request to every
 * five-second tick to watch something that rarely moves is not a trade worth
 * making.
 */
function BranchMenu({
  path,
  status,
  busy,
  onSwitch,
}: {
  path: string;
  status: GitStatus;
  busy: boolean;
  onSwitch: (name: string, create: boolean) => void;
}) {
  const [open, setOpen] = useState(false);
  const [branches, setBranches] = useState<GitBranch[]>([]);
  const [fresh, setFresh] = useState("");
  const wrap = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    gitBranches(path)
      .then((b) => setBranches(b.branches))
      .catch(() => setBranches([]));
  }, [open, path, status.branch]);

  useEffect(() => {
    if (!open) return;
    function onPointer(e: PointerEvent) {
      if (!wrap.current?.contains(e.target as Node)) setOpen(false);
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("pointerdown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const label = status.branch ?? (status.detached ? "detached" : "no branch");

  return (
    <div ref={wrap} style={{ position: "relative", flexShrink: 0 }}>
      <button
        onClick={() => setOpen((v) => !v)}
        disabled={busy}
        title={status.root ?? undefined}
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 7,
          padding: "5px 11px",
          borderRadius: 8,
          border: "1px solid " + (open ? "rgba(var(--accent2-rgb), 0.4)" : C.border),
          background: open ? "rgba(var(--accent2-rgb), 0.12)" : "transparent",
          color: C.text,
          fontFamily: MONO,
          fontSize: 14,
          cursor: "pointer",
        }}
      >
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke={C.accent} strokeWidth="1.8" aria-hidden="true">
          <circle cx="6" cy="6" r="3" />
          <circle cx="6" cy="18" r="3" />
          <path d="M6 9v6M18 8v3a3 3 0 0 1-3 3H9" />
          <circle cx="18" cy="5" r="3" />
        </svg>
        {label}
        <span style={{ color: C.textFaint, fontSize: 11 }}>▾</span>
      </button>

      {open && (
        <div style={branchMenuStyle}>
          <div style={{ maxHeight: 240, overflowY: "auto", padding: "5px 0" }}>
            {branches.map((b) => (
              <button
                key={b.name}
                disabled={b.current}
                onClick={() => {
                  setOpen(false);
                  onSwitch(b.name, false);
                }}
                style={{
                  ...branchItemStyle,
                  color: b.current ? C.accent2 : "var(--text)",
                  cursor: b.current ? "default" : "pointer",
                }}
                title={`${b.short} ${b.subject}`}
              >
                <span style={{ width: 10, flexShrink: 0 }}>{b.current ? "•" : ""}</span>
                <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {b.name}
                </span>
                {b.upstream && (
                  <span style={{ marginLeft: "auto", flexShrink: 0, fontSize: 12, color: C.textFaint }}>
                    tracking
                  </span>
                )}
              </button>
            ))}
            {branches.length === 0 && (
              <div style={{ padding: "8px 12px", fontSize: 13, color: C.textFaint }}>
                No branches yet - there is nothing committed.
              </div>
            )}
          </div>

          <div style={{ borderTop: `1px solid ${C.border}`, padding: 9 }}>
            {/* Creating carries uncommitted work onto the new branch, which is
                the point of it. Switching to an existing one while the tree is
                dirty is refused by the server, and says why. */}
            <input
              value={fresh}
              onChange={(e) => setFresh(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && fresh.trim()) {
                  setOpen(false);
                  onSwitch(fresh.trim(), true);
                  setFresh("");
                }
              }}
              placeholder="new-branch-from-here"
              style={{ ...inputStyle, width: "100%", boxSizing: "border-box" }}
            />
            <div style={{ fontSize: 12.5, color: C.textFaint, marginTop: 7, lineHeight: 1.5 }}>
              Enter to create it here. Anything uncommitted comes with you.
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * "I have a local repository and I want it on GitHub."
 *
 * Adding the remote is all this does. The first push stays a separate press,
 * so nothing leaves the machine as a side effect of filling in a URL.
 */
function PublishForm({
  busy,
  onSave,
  onCancel,
}: {
  busy: boolean;
  onSave: (url: string) => void;
  onCancel: () => void;
}) {
  const [url, setUrl] = useState("");
  const ready = url.trim().length > 0 && !busy;
  return (
    <div
      style={{
        flexShrink: 0,
        display: "flex",
        alignItems: "center",
        gap: 10,
        flexWrap: "wrap",
        padding: "11px 20px",
        borderBottom: `1px solid ${C.border}`,
        background: "rgba(var(--accent2-rgb), 0.06)",
      }}
    >
      <span style={{ fontSize: 13.5, color: C.textDim }}>
        Where should this push to? Create an empty repository on GitHub or GitLab and
        paste its URL. Adding it does not push anything yet.
      </span>
      <input
        value={url}
        onChange={(e) => setUrl(e.target.value)}
        placeholder="https://github.com/you/your-brain.git"
        style={{ ...inputStyle, minWidth: 320, flex: 1 }}
      />
      <button disabled={!ready} onClick={() => onSave(url.trim())} style={primaryStyle(ready)}>
        Add remote
      </button>
      <button onClick={onCancel} style={ghostStyle}>
        Cancel
      </button>
    </div>
  );
}

function Banner({ text }: { text: string }) {
  return (
    <div
      style={{
        flexShrink: 0,
        padding: "9px 20px",
        borderBottom: `1px solid ${C.border}`,
        background: "rgba(var(--accent3-rgb), 0.06)",
        color: C.textDim,
        fontSize: 13.5,
      }}
    >
      {text}
    </div>
  );
}

/**
 * Asked for up front rather than at the moment of the commit.
 *
 * git with no user.name / user.email fails at `git commit` with a paragraph
 * about --global, which lands exactly when someone is trying to save work.
 * Knowing before the button is pressed turns that into one form. It writes to
 * the repository's own config: Genesis is in no position to decide what
 * identity belongs on every repo on the machine.
 */
function IdentityForm({ busy, onSave }: { busy: boolean; onSave: (n: string, e: string) => void }) {
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const ready = name.trim().length > 0 && email.trim().length > 0 && !busy;
  return (
    <div
      style={{
        flexShrink: 0,
        display: "flex",
        alignItems: "center",
        gap: 10,
        flexWrap: "wrap",
        padding: "11px 20px",
        borderBottom: `1px solid ${C.border}`,
        background: "rgba(var(--accent3-rgb), 0.07)",
      }}
    >
      <span style={{ fontSize: 13.5, color: C.textDim }}>
        git has no name or email set, so there is nobody to attribute commits to. This
        sets them for this repository only.
      </span>
      <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Ada Lovelace" style={{ ...inputStyle, width: 170 }} />
      <input value={email} onChange={(e) => setEmail(e.target.value)} placeholder="ada@example.com" style={{ ...inputStyle, width: 210 }} />
      <button disabled={!ready} onClick={() => onSave(name.trim(), email.trim())} style={primaryStyle(ready)}>
        Save
      </button>
    </div>
  );
}

/* ──────────────────────────  the Changes half  ─────────────────────── */

function Changes({
  path,
  status,
  busy,
  onStage,
  onCommit,
  onRestore,
}: {
  path: string;
  status: GitStatus;
  busy: boolean;
  onStage: (files: string[], staged: boolean) => void;
  onCommit: (message: string, stageAll: boolean) => void;
  onRestore: (file: string, sha?: string) => void;
}) {
  const [selected, setSelected] = useState<string | null>(null);
  const [diff, setDiff] = useState<GitDiff | null>(null);
  const [diffError, setDiffError] = useState<string | null>(null);
  const [message, setMessage] = useState("");

  const active = useMemo(
    () => status.files.find((f) => f.file === selected) ?? null,
    [status.files, selected],
  );

  // Depends on the whole `active` row, not just its name: staging a file
  // moves what "the diff" even means from the worktree to the index, and the
  // row is a fresh object on every poll, so a file edited outside Genesis
  // re-reads too. One local request every few seconds, in exchange for a
  // pane that is never quietly showing the previous answer.
  useEffect(() => {
    if (!active) {
      setDiff(null);
      return;
    }
    let cancelled = false;
    setDiffError(null);
    gitDiff({ path, file: active.file, staged: active.staged && !active.unstaged })
      .then((d) => !cancelled && setDiff(d))
      .catch((e) => !cancelled && setDiffError((e as InitError).error || "Couldn't read that diff."));
    return () => {
      cancelled = true;
    };
  }, [path, active]);

  const inProject = status.files.filter((f) => f.rel !== null);
  const elsewhere = status.files.filter((f) => f.rel === null);
  const repoName = status.root?.split(/[\\/]/).filter(Boolean).pop() ?? "the repository";

  return (
    <div style={{ flex: 1, display: "flex", minHeight: 0 }}>
      <div style={railStyle}>
        <div style={{ flex: 1, overflowY: "auto", padding: "14px 12px" }}>
          {status.clean && (
            <div style={{ ...noteStyle, margin: 4 }}>
              Nothing has changed since {status.head ? `${status.head.short}.` : "the last commit."}{" "}
              Add or edit a component and it will show up here.
            </div>
          )}

          {inProject.length > 0 && <RailHeading text={`${fileCount(inProject.length)} in this project`} />}
          {inProject.map((f) => (
            <FileRow key={f.file} file={f} active={f.file === selected} busy={busy} onSelect={setSelected} onStage={onStage} />
          ))}

          {elsewhere.length > 0 && (
            <>
              {/* Status is repo-wide because `git commit` is. Hiding these
                  would understate what the commit button is about to do. */}
              <RailHeading text={`elsewhere in ${repoName}`} />
              {elsewhere.map((f) => (
                <FileRow key={f.file} file={f} active={f.file === selected} busy={busy} onSelect={setSelected} onStage={onStage} />
              ))}
            </>
          )}
        </div>

        <CommitBox
          status={status}
          busy={busy}
          message={message}
          onMessage={setMessage}
          onCommit={(stageAll) => {
            onCommit(message.trim(), stageAll);
            setMessage("");
          }}
        />
      </div>

      <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column" }}>
        {!active && (
          <div style={{ padding: 24 }}>
            <div style={noteStyle}>
              Pick a file to see what changed in it. The checkbox on each row stages it;
              the commit box takes what is staged.
            </div>
          </div>
        )}

        {active && (
          <>
            <div style={paneBarStyle}>
              <span style={{ fontFamily: MONO, fontSize: 14, color: C.text, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {active.rel ?? active.file}
              </span>
              <span style={{ fontSize: 13, color: C.textFaint }}>
                {active.staged && !active.unstaged ? "staged" : active.label}
                {active.origin ? ` from ${active.origin}` : ""}
              </span>
              {/* Restore is the destructive end, so it says what it will do
                  and names the one file it can touch. */}
              <button
                disabled={busy || active.untracked}
                onClick={() => onRestore(active.file)}
                title={
                  active.untracked
                    ? "git has no version of this file to restore it to."
                    : `Overwrite ${active.rel ?? active.file} with its content at the last commit. Nothing else is touched.`
                }
                style={{ ...ghostStyle, marginLeft: "auto", color: active.untracked ? C.textFaint : C.danger }}
              >
                Discard changes
              </button>
            </div>
            <DiffPane diff={diff} error={diffError} />
          </>
        )}
      </div>
    </div>
  );
}

function fileCount(n: number): string {
  return `${n} file${n === 1 ? "" : "s"}`;
}

function RailHeading({ text }: { text: string }) {
  return (
    <div
      style={{
        fontSize: 12.5,
        color: C.textFaint,
        fontWeight: 600,
        textTransform: "uppercase",
        letterSpacing: "0.08em",
        margin: "12px 4px 8px",
      }}
    >
      {text}
    </div>
  );
}

function FileRow({
  file,
  active,
  busy,
  onSelect,
  onStage,
}: {
  file: GitFile;
  active: boolean;
  busy: boolean;
  onSelect: (f: string) => void;
  onStage: (files: string[], staged: boolean) => void;
}) {
  const tone = file.conflicted
    ? C.danger
    : file.untracked
      ? C.receptor
      : file.label === "deleted"
        ? C.accent3
        : C.warn;
  return (
    <div
      onClick={() => onSelect(file.file)}
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        border: `1px solid ${active ? "rgba(var(--accent2-rgb), 0.4)" : C.border}`,
        background: active ? "rgba(var(--accent2-rgb), 0.1)" : "transparent",
        borderRadius: 9,
        padding: "7px 9px",
        marginBottom: 6,
        cursor: "pointer",
      }}
    >
      <input
        type="checkbox"
        checked={file.staged}
        disabled={busy}
        onClick={(e) => e.stopPropagation()}
        onChange={(e) => onStage([file.file], e.target.checked)}
        title={file.staged ? "Staged - uncheck to take it out of the next commit" : "Stage this file"}
        style={{ accentColor: C.accent2, flexShrink: 0 }}
      />
      <span
        style={{
          fontFamily: MONO,
          fontSize: 13.5,
          color: C.text,
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
          direction: "rtl",
          textAlign: "left",
        }}
        title={file.file}
      >
        {file.rel ?? file.file}
      </span>
      <span style={{ marginLeft: "auto", flexShrink: 0, fontSize: 12.5, color: tone }}>{file.label}</span>
    </div>
  );
}

/**
 * The commit box. Two buttons rather than one, because the difference between
 * them is the difference between "what I ticked" and "everything", and hiding
 * that inside a single button is how people commit things they hadn't looked
 * at.
 */
function CommitBox({
  status,
  busy,
  message,
  onMessage,
  onCommit,
}: {
  status: GitStatus;
  busy: boolean;
  message: string;
  onMessage: (v: string) => void;
  onCommit: (stageAll: boolean) => void;
}) {
  const ready = message.trim().length > 0 && !busy;
  return (
    <div style={{ flexShrink: 0, borderTop: `1px solid ${C.border}`, padding: 12 }}>
      <textarea
        value={message}
        onChange={(e) => onMessage(e.target.value)}
        placeholder="What changed, and why"
        rows={2}
        style={{ ...inputStyle, width: "100%", resize: "vertical", marginBottom: 8 }}
      />
      <div style={{ display: "flex", gap: 8 }}>
        <button
          disabled={!ready || status.staged === 0}
          onClick={() => onCommit(false)}
          title={status.staged === 0 ? "Nothing is staged yet." : `Commit the ${status.staged} staged file(s).`}
          style={{ ...primaryStyle(ready && status.staged > 0), flex: 1 }}
        >
          Commit {status.staged > 0 ? `(${status.staged})` : ""}
        </button>
        <button
          disabled={!ready || status.clean}
          onClick={() => onCommit(true)}
          title="Stage everything in the repository, then commit it - the one-click checkpoint."
          style={{ ...ghostStyle, flexShrink: 0 }}
        >
          Stage all &amp; commit
        </button>
      </div>
    </div>
  );
}

/* ──────────────────────────  the History half  ─────────────────────── */

function History({ path }: { path: string }) {
  const [commits, setCommits] = useState<GitCommit[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<GitCommitDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    gitLog(path)
      .then((l) => setCommits(l.commits))
      .catch((e) => setError((e as InitError).error || "Couldn't read the log."));
  }, [path]);

  useEffect(() => {
    if (!selected) return;
    let cancelled = false;
    // Cleared first: a diff pane still showing the previous commit while the
    // next one loads is the one moment this panel could mislead about which
    // change you're looking at.
    setDetail(null);
    gitShow(path, selected)
      .then((d) => !cancelled && setDetail(d))
      .catch((e) => !cancelled && setError((e as InitError).error || "Couldn't read that commit."));
    return () => {
      cancelled = true;
    };
  }, [path, selected]);

  return (
    <div style={{ flex: 1, display: "flex", minHeight: 0 }}>
      <div style={{ ...railStyle, overflowY: "auto", padding: "14px 12px" }}>
        {commits.length === 0 && (
          <div style={{ ...noteStyle, margin: 4 }}>
            {error ?? "No commits yet. Make one from the Changes tab and it will appear here."}
          </div>
        )}
        {commits.map((c) => {
          const on = c.sha === selected;
          return (
            <div
              key={c.sha}
              onClick={() => setSelected(c.sha)}
              style={{
                border: `1px solid ${on ? "rgba(var(--accent2-rgb), 0.4)" : C.border}`,
                background: on ? "rgba(var(--accent2-rgb), 0.1)" : "transparent",
                borderRadius: 9,
                padding: "9px 10px",
                marginBottom: 6,
                cursor: "pointer",
              }}
            >
              <div style={{ fontSize: 14, color: C.text, lineHeight: 1.4 }}>{c.subject}</div>
              <div style={{ display: "flex", gap: 8, marginTop: 5, fontFamily: MONO, fontSize: 12.5, color: C.textFaint }}>
                <span style={{ color: C.accent2 }}>{c.short}</span>
                <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{c.author}</span>
                <span style={{ marginLeft: "auto", flexShrink: 0 }}>{fmtDate(c.date)}</span>
              </div>
            </div>
          );
        })}
      </div>

      <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column" }}>
        {!detail && (
          <div style={{ padding: 24 }}>
            <div style={noteStyle}>Pick a commit to see everything it changed.</div>
          </div>
        )}
        {detail && (
          <>
            <div style={{ ...paneBarStyle, flexWrap: "wrap" }}>
              <span style={{ fontFamily: MONO, fontSize: 14, color: C.accent2 }}>{detail.commit.short}</span>
              <span style={{ fontSize: 14, color: C.text }}>{detail.commit.subject}</span>
              <span style={{ fontSize: 13, color: C.textFaint }}>
                {detail.commit.author} · {fmtDate(detail.commit.date)}
                {detail.commit.root ? " · root commit" : ""}
              </span>
              <span style={{ marginLeft: "auto", fontSize: 13, color: C.textFaint }}>
                {detail.changes.length} file{detail.changes.length === 1 ? "" : "s"}
                {" · "}
                <span style={{ color: C.ok }}>+{sum(detail.changes.map((c) => c.added))}</span>{" "}
                <span style={{ color: C.danger }}>-{sum(detail.changes.map((c) => c.removed))}</span>
              </span>
            </div>
            <DiffPane
              diff={{
                file: detail.commit.short,
                rel: null,
                diff: detail.diff,
                binary: false,
                truncated: detail.truncated,
                empty: !detail.diff.trim(),
                sha: detail.commit.sha,
                staged: false,
              }}
              error={null}
            />
          </>
        )}
      </div>
    </div>
  );
}

function sum(xs: (number | null)[]): number {
  return xs.reduce<number>((a, b) => a + (b ?? 0), 0);
}

/* ────────────────────────────  the diff  ───────────────────────────── */

/**
 * Unified diff, coloured by the first character of each line.
 *
 * Deliberately not a syntax highlighter. In a diff the thing you are reading
 * for is which side a line is on, and colouring the language on top of that
 * competes with the only distinction that matters here.
 */
function DiffPane({ diff, error }: { diff: GitDiff | null; error: string | null }) {
  if (error) {
    return (
      <div style={{ padding: 24 }}>
        <div style={errorStyle}>{error}</div>
      </div>
    );
  }
  if (!diff) {
    return <div style={{ padding: 24, color: C.textFaint, fontFamily: MONO, fontSize: 14 }}>Reading…</div>;
  }
  if (diff.binary) {
    return (
      <div style={{ padding: 24 }}>
        <div style={noteStyle}>This is a binary file, so there are no lines to compare.</div>
      </div>
    );
  }
  if (diff.empty) {
    return (
      <div style={{ padding: 24 }}>
        <div style={noteStyle}>
          Nothing to show. The change here is to the file itself - a rename, a mode
          change, or content already identical to what is committed.
        </div>
      </div>
    );
  }

  const lines = diff.diff.split("\n");
  return (
    <div style={{ flex: 1, minHeight: 0, overflow: "auto", background: "var(--bg-well)" }}>
      <pre style={preStyle}>
        {lines.map((line, i) => (
          <div key={i} style={lineStyle(line)}>
            {line || " "}
          </div>
        ))}
      </pre>
      {diff.truncated && (
        <div style={{ padding: "10px 16px", fontSize: 13, color: C.warn, fontFamily: MONO }}>
          Truncated - this diff is too large to show in full. `git show` it in a terminal
          for the rest.
        </div>
      )}
    </div>
  );
}

function lineStyle(line: string): CSSProperties {
  const base: CSSProperties = { padding: "0 16px", whiteSpace: "pre-wrap", wordBreak: "break-word" };
  if (line.startsWith("+++") || line.startsWith("---"))
    return { ...base, color: C.textFaint };
  if (line.startsWith("@@")) return { ...base, color: C.accent2, background: "rgba(var(--accent2-rgb), 0.07)" };
  if (line.startsWith("+")) return { ...base, color: C.ok, background: "rgba(var(--accent2-rgb), 0.05)" };
  if (line.startsWith("-")) return { ...base, color: C.danger, background: "rgba(var(--danger-rgb), 0.07)" };
  if (line.startsWith("diff --git") || line.startsWith("index ") || line.startsWith("new file") || line.startsWith("deleted file"))
    return { ...base, color: C.textDim, fontWeight: 600 };
  return { ...base, color: C.textDim };
}

/** ISO-8601 in, something a human reads out. */
function fmtDate(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const mins = Math.round((Date.now() - d.getTime()) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days < 30) return `${days}d ago`;
  return d.toLocaleDateString();
}

/* ────────────────────────────  styles  ─────────────────────────────── */

const railStyle: CSSProperties = {
  width: 320,
  flexShrink: 0,
  display: "flex",
  flexDirection: "column",
  borderRight: `1px solid ${C.border}`,
  background: "var(--bg-rail)",
  minHeight: 0,
};

const branchMenuStyle: CSSProperties = {
  position: "absolute",
  top: "calc(100% + 8px)",
  left: 0,
  zIndex: 40,
  width: 268,
  background: "var(--bg-overlay)",
  border: "1px solid var(--border-strong)",
  borderRadius: 11,
  boxShadow: "0 18px 46px rgba(var(--shadow-rgb), 0.45)",
  WebkitBackdropFilter: "blur(18px)",
  backdropFilter: "blur(18px)",
};

const branchItemStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  width: "100%",
  padding: "7px 12px",
  background: "transparent",
  border: "none",
  fontFamily: MONO,
  fontSize: 13.5,
  textAlign: "left",
};

const paneBarStyle: CSSProperties = {
  flexShrink: 0,
  display: "flex",
  alignItems: "center",
  gap: 10,
  padding: "10px 16px",
  borderBottom: `1px solid ${C.border}`,
};

const preStyle: CSSProperties = {
  margin: 0,
  padding: "12px 0",
  fontFamily: MONO,
  fontSize: 13.5,
  lineHeight: 1.55,
};

const cardStyle: CSSProperties = {
  background: "var(--bg-card)",
  border: `1px solid ${C.border}`,
  borderRadius: 14,
  padding: 22,
};

const noteStyle: CSSProperties = {
  border: `1px solid ${C.border}`,
  borderRadius: 10,
  padding: "12px 14px",
  fontSize: 13.5,
  lineHeight: 1.6,
  color: C.textDim,
  background: "var(--bg-well)",
};

const errorStyle: CSSProperties = {
  border: "1px solid rgba(var(--accent3-rgb), 0.35)",
  background: "rgba(var(--accent3-rgb), 0.08)",
  borderRadius: 10,
  padding: "12px 14px",
  fontSize: 13.5,
  lineHeight: 1.6,
  color: C.accent3,
};

const inputStyle: CSSProperties = {
  background: "var(--bg-well)",
  border: `1px solid ${C.border}`,
  borderRadius: 8,
  padding: "7px 10px",
  color: "var(--text)",
  fontFamily: MONO,
  fontSize: 13.5,
  outline: "none",
};

function primaryStyle(enabled: boolean): CSSProperties {
  return {
    padding: "7px 16px",
    borderRadius: 9,
    border: "none",
    background: enabled ? `linear-gradient(90deg, ${C.accent}, ${C.accent2})` : "var(--bg-well)",
    color: enabled ? C.onPrimary : C.textFaint,
    fontFamily: MONO,
    fontSize: 14,
    fontWeight: 600,
    cursor: enabled ? "pointer" : "default",
  };
}

const ghostStyle: CSSProperties = {
  padding: "6px 13px",
  borderRadius: 8,
  border: `1px solid ${C.border}`,
  background: "transparent",
  color: "var(--text-dim)",
  fontFamily: MONO,
  fontSize: 13.5,
  cursor: "pointer",
};
