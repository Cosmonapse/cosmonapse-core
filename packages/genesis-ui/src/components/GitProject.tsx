import { useCallback, useEffect, useState } from "react";
import type { CSSProperties } from "react";
import {
  detect,
  forgeConnect,
  forgeDisconnect,
  forgeRepos,
  forgeStatus,
  gitClone,
} from "../api";
import { C, MONO } from "../theme";
import type {
  Detection,
  ForgeAccount,
  ForgeKind,
  ForgeRepo,
  InitError,
} from "../types";
import { FolderBrowser, pushRecent } from "./FolderBrowser";

/**
 * The Git half of the start screen: connect an account, pick a repository,
 * clone it somewhere, open it.
 *
 * The account is not Genesis'. A token pasted here is checked against the
 * host and then handed to `git credential approve`, which puts it wherever
 * the user's own credential helper keeps secrets - Windows Credential
 * Manager, the macOS Keychain, libsecret. Genesis remembers a host and a
 * login and nothing else, and asks git for the token back when it needs to
 * list repositories. Three things follow: the same credential works for a
 * push typed into a terminal, revoking it in one place revokes it
 * everywhere, and uninstalling Genesis leaves no secret behind.
 *
 * Which is also why the "no credential helper" case is a wall rather than a
 * warning. With no helper, `git credential approve` succeeds and stores
 * nothing - the token would appear to save and then fail at the first push,
 * a long way from the form that caused it.
 */
export function GitProject({
  onOpen,
}: {
  onOpen: (path: string, name: string) => void;
}) {
  const [account, setAccount] = useState<ForgeAccount | null>(null);
  const [cloned, setCloned] = useState<{ path: string; probe: Detection } | null>(null);

  const refresh = useCallback(() => {
    forgeStatus()
      .then(setAccount)
      .catch(() => setAccount(null));
  }, []);

  useEffect(refresh, [refresh]);

  if (cloned) {
    return (
      <Cloned
        path={cloned.path}
        probe={cloned.probe}
        onOpen={onOpen}
        onBack={() => setCloned(null)}
      />
    );
  }

  if (!account) {
    return <div style={mutedStyle}>Checking for a git account…</div>;
  }

  if (!account.connected) {
    return <Connect account={account} onConnected={setAccount} />;
  }

  return (
    <Clone
      account={account}
      onCloned={(path, probe) => setCloned({ path, probe })}
      onDisconnect={() => forgeDisconnect().then(setAccount).catch(refresh)}
    />
  );
}

/* ─────────────────────────  connecting an account  ────────────────────── */

const KIND_LABEL: Record<ForgeKind, string> = {
  github: "GitHub",
  gitlab: "GitLab",
  other: "Other",
};

function Connect({
  account,
  onConnected,
}: {
  account: ForgeAccount;
  onConnected: (a: ForgeAccount) => void;
}) {
  const [kind, setKind] = useState<ForgeKind>("github");
  const [baseUrl, setBaseUrl] = useState("");
  const [token, setToken] = useState("");
  const [store, setStore] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const help = account.token_help[kind];
  const needsBase = kind === "other";
  const ready =
    token.trim().length > 0 &&
    (!needsBase || baseUrl.trim().length > 0) &&
    (!account.can_store || store) &&
    !busy;

  async function connect() {
    setBusy(true);
    setError(null);
    try {
      onConnected(
        await forgeConnect({
          kind,
          token: token.trim(),
          base_url: baseUrl.trim(),
          enable_store: store,
        }),
      );
      // Not kept in component state a moment longer than it has to be. It is
      // in git's credential store now, and this field has no further use.
      setToken("");
    } catch (e) {
      setError((e as InitError).error || "Couldn't connect that account.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <p style={leadStyle}>
        Connect a GitHub or GitLab account to clone a repository straight into a
        folder and work on it here. Genesis hands the token to git's own credential
        store and keeps none of it, so the same login works from your terminal.
      </p>

      {account.reason && !account.can_store && <div style={errorStyle}>{account.reason}</div>}

      <Field label="Host">
        <div style={{ display: "flex", gap: 6 }}>
          {account.kinds.map((k) => {
            const on = k === kind;
            return (
              <div
                key={k}
                onClick={() => setKind(k)}
                style={{
                  padding: "5px 14px",
                  borderRadius: 8,
                  cursor: "pointer",
                  fontFamily: MONO,
                  fontSize: 14,
                  color: on ? C.accent2 : C.textDim,
                  background: on ? "rgba(var(--accent2-rgb), 0.12)" : "transparent",
                  border: "1px solid " + (on ? "rgba(var(--accent2-rgb), 0.4)" : C.border),
                }}
              >
                {KIND_LABEL[k]}
              </div>
            );
          })}
        </div>
      </Field>

      {needsBase && (
        <Field label="Server address">
          <input
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            placeholder="https://git.example.com"
            style={inputStyle}
          />
          <div style={hintStyle}>
            Genesis can store credentials for any git server, but only GitHub and
            GitLab can be asked for a list of your repositories. For anything else,
            paste a clone URL on the next screen.
          </div>
        </Field>
      )}

      <Field label="Personal access token">
        <input
          type="password"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          placeholder="paste it here"
          autoComplete="off"
          spellCheck={false}
          style={inputStyle}
        />
        {help && (
          <div style={hintStyle}>
            Needs the <code style={codeStyle}>{help.scopes}</code> scope.{" "}
            <a href={help.url} target="_blank" rel="noreferrer" style={linkStyle}>
              Create one on {KIND_LABEL[kind]}
            </a>
            . It is checked before it is stored, so a wrong one fails here rather
            than at your first push.
          </div>
        )}
      </Field>

      {/* The wall, not a warning. See this component's docstring. */}
      {account.can_store && (
        <label style={checkStyle}>
          <input
            type="checkbox"
            checked={store}
            onChange={(e) => setStore(e.target.checked)}
            style={{ accentColor: C.accent2 }}
          />
          <span style={{ fontFamily: MONO, fontSize: 14, color: C.text }}>
            Turn on git's plaintext credential store
          </span>
          <span style={{ fontSize: 13, color: C.textFaint, fontWeight: 600 }}>
            this machine has no credential helper, so there is nowhere else for a
            token to go. It would be saved unencrypted in ~/.git-credentials.
          </span>
        </label>
      )}

      {error && <div style={errorStyle}>{error}</div>}

      <button disabled={!ready} onClick={connect} style={primaryStyle(ready)}>
        {busy ? "Checking…" : "Connect"}
      </button>
    </div>
  );
}

/* ────────────────────────────  picking a repo  ────────────────────────── */

function Clone({
  account,
  onCloned,
  onDisconnect,
}: {
  account: ForgeAccount;
  onCloned: (path: string, probe: Detection) => void;
  onDisconnect: () => void;
}) {
  const [repos, setRepos] = useState<ForgeRepo[] | null>(null);
  const [query, setQuery] = useState("");
  const [picked, setPicked] = useState<ForgeRepo | null>(null);
  const [url, setUrl] = useState("");
  const [folder, setFolder] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [listError, setListError] = useState<string | null>(null);

  useEffect(() => {
    forgeRepos()
      .then((r) => setRepos(r.repos))
      .catch((e) => {
        setRepos([]);
        setListError((e as InitError).error || null);
      });
  }, []);

  const target = picked?.url ?? url.trim();
  const name = picked?.name ?? repoName(url);
  const ready = target.length > 0 && folder.trim().length > 0 && !busy;

  const shown = (repos ?? []).filter((r) => {
    const needle = query.trim().toLowerCase();
    return (
      !needle ||
      r.full_name.toLowerCase().includes(needle) ||
      r.description.toLowerCase().includes(needle)
    );
  });

  async function clone() {
    setBusy(true);
    setError(null);
    try {
      const status = await gitClone({ path: folder, url: target, name });
      const into = status.cloned_to ?? "";
      pushRecent(folder);
      // Probed rather than assumed: a repository can be perfectly good and
      // still not be a Cosmonapse project, and telling someone that up front
      // beats opening an empty canvas at them.
      onCloned(into, await detect(into));
    } catch (e) {
      setError((e as InitError).error || "Couldn't clone that repository.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <div style={accountBarStyle}>
        <span style={{ fontFamily: MONO, fontSize: 14, color: C.text }}>
          {account.login}
          <span style={{ color: C.textFaint }}> on {account.host}</span>
        </span>
        <button onClick={onDisconnect} style={{ ...ghostStyle, marginLeft: "auto" }}>
          Disconnect
        </button>
      </div>

      {repos === null && <div style={mutedStyle}>Reading your repositories…</div>}

      {repos !== null && repos.length > 0 && (
        <Field label={`Repository · ${shown.length} of ${repos.length}`}>
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Filter by name or description"
            style={{ ...inputStyle, marginBottom: 8 }}
          />
          <div style={listStyle}>
            {shown.map((r) => {
              const on = r.full_name === picked?.full_name;
              return (
                <div
                  key={r.full_name}
                  onClick={() => {
                    setPicked(r);
                    setUrl("");
                  }}
                  style={{
                    padding: "8px 10px",
                    borderRadius: 8,
                    cursor: "pointer",
                    marginBottom: 4,
                    border: "1px solid " + (on ? "rgba(var(--accent2-rgb), 0.4)" : "transparent"),
                    background: on ? "rgba(var(--accent2-rgb), 0.1)" : "transparent",
                  }}
                >
                  <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <span style={{ fontFamily: MONO, fontSize: 13.5, color: C.text }}>
                      {r.full_name}
                    </span>
                    {r.private && <span style={badgeStyle}>private</span>}
                  </div>
                  {r.description && (
                    <div
                      style={{
                        fontSize: 13,
                        color: C.textFaint,
                        marginTop: 3,
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {r.description}
                    </div>
                  )}
                </div>
              );
            })}
            {shown.length === 0 && (
              <div style={{ ...hintStyle, padding: 8 }}>Nothing matches that.</div>
            )}
          </div>
        </Field>
      )}

      {/* Always available, not just as a fallback: a repository you have
          access to but do not own may not be in the list at all. */}
      <Field label={repos && repos.length > 0 ? "…or paste a clone URL" : "Clone URL"}>
        <input
          value={url}
          onChange={(e) => {
            setUrl(e.target.value);
            setPicked(null);
          }}
          placeholder="https://github.com/you/your-brain.git"
          style={inputStyle}
        />
        {listError && <div style={hintStyle}>{listError}</div>}
      </Field>

      <Field label="Clone into">
        <input
          value={folder}
          onChange={(e) => setFolder(e.target.value)}
          placeholder="/path/to/folder"
          style={{ ...inputStyle, marginBottom: 10 }}
        />
        <FolderBrowser path={folder} onChange={setFolder} />
        {target && folder && (
          <div style={hintStyle}>
            It will land in a new folder: <code style={codeStyle}>{name}</code> inside
            the one above. Nothing already there is touched.
          </div>
        )}
      </Field>

      {error && <div style={errorStyle}>{error}</div>}

      <button disabled={!ready} onClick={clone} style={primaryStyle(ready)}>
        {busy ? "Cloning…" : target ? `Clone ${name}` : "Clone"}
      </button>
    </div>
  );
}

function repoName(url: string): string {
  const tail = url.trim().replace(/\/+$/, "").split(/[/:]/).pop() ?? "";
  return tail.replace(/\.git$/, "");
}

/* ────────────────────────────  after the clone  ───────────────────────── */

/**
 * A cloned repository is not automatically a Cosmonapse project, and saying
 * so here is kinder than opening a canvas with nothing on it. Either way the
 * clone worked and the folder is on disk, so "Open anyway" is always offered
 * - Genesis can read a folder it did not scaffold.
 */
function Cloned({
  path,
  probe,
  onOpen,
  onBack,
}: {
  path: string;
  probe: Detection;
  onOpen: (path: string, name: string) => void;
  onBack: () => void;
}) {
  const name = path.split(/[\\/]/).filter(Boolean).pop() ?? path;
  return (
    <div>
      <div style={{ ...verdictStyle, borderColor: "rgba(var(--accent2-rgb), 0.3)" }}>
        <div style={{ fontFamily: MONO, fontSize: 14.5, color: C.accent2 }}>
          Cloned into {name}
        </div>
        <div style={{ fontSize: 13, color: C.textFaint, marginTop: 6, wordBreak: "break-all" }}>
          {path}
        </div>
        <div style={{ fontSize: 13.5, color: C.textDim, marginTop: 10, lineHeight: 1.6 }}>
          {probe.is_project
            ? "It's a Cosmonapse project, so the canvas and the Code tab will have something to show."
            : `${probe.reason} You can still open it - the History tab works on any repository, and you can scaffold a brain into it from the Local tab.`}
        </div>
      </div>

      <div style={{ display: "flex", gap: 8 }}>
        <button
          onClick={() => onOpen(path, name)}
          style={{ ...primaryStyle(true), flex: 1 }}
        >
          Open {name}
        </button>
        <button onClick={onBack} style={ghostStyle}>
          Clone another
        </button>
      </div>
    </div>
  );
}

/* ────────────────────────────────  bits  ──────────────────────────────── */

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 16 }}>
      <div
        style={{
          fontSize: 12.5,
          color: C.textFaint,
          fontWeight: 600,
          textTransform: "uppercase",
          letterSpacing: "0.08em",
          marginBottom: 7,
        }}
      >
        {label}
      </div>
      {children}
    </div>
  );
}

const leadStyle: CSSProperties = {
  color: C.textDim,
  fontWeight: 600,
  fontSize: 14.5,
  lineHeight: 1.6,
  margin: "0 0 18px",
};

const mutedStyle: CSSProperties = {
  color: C.textFaint,
  fontFamily: MONO,
  fontSize: 14,
  padding: "18px 0",
};

const accountBarStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 10,
  padding: "9px 12px",
  marginBottom: 16,
  borderRadius: 10,
  border: `1px solid ${C.border}`,
  background: "var(--bg-well)",
};

const listStyle: CSSProperties = {
  maxHeight: 208,
  overflowY: "auto",
  border: `1px solid ${C.border}`,
  borderRadius: 10,
  padding: 5,
  background: "var(--bg-well)",
};

const badgeStyle: CSSProperties = {
  fontSize: 11.5,
  fontFamily: MONO,
  color: C.textFaint,
  border: `1px solid ${C.border}`,
  borderRadius: 5,
  padding: "1px 5px",
};

const verdictStyle: CSSProperties = {
  border: `1px solid ${C.border}`,
  borderRadius: 11,
  padding: "13px 15px",
  marginBottom: 16,
  background: "rgba(var(--accent2-rgb), 0.05)",
};

const inputStyle: CSSProperties = {
  width: "100%",
  boxSizing: "border-box",
  background: "var(--bg-well)",
  border: `1px solid ${C.border}`,
  borderRadius: 9,
  padding: "9px 12px",
  color: "var(--text)",
  fontFamily: MONO,
  fontSize: 14,
  outline: "none",
};

const hintStyle: CSSProperties = {
  fontSize: 13,
  color: C.textFaint,
  fontWeight: 600,
  lineHeight: 1.6,
  marginTop: 7,
};

const codeStyle: CSSProperties = {
  fontFamily: MONO,
  fontSize: 12.5,
  color: C.accent2,
};

const linkStyle: CSSProperties = {
  color: C.accent2,
  textDecoration: "none",
};

const checkStyle: CSSProperties = {
  display: "flex",
  alignItems: "baseline",
  gap: 9,
  flexWrap: "wrap",
  margin: "0 0 16px",
  cursor: "pointer",
};

const errorStyle: CSSProperties = {
  border: "1px solid rgba(var(--accent3-rgb), 0.35)",
  background: "rgba(var(--accent3-rgb), 0.08)",
  borderRadius: 10,
  padding: "11px 13px",
  fontSize: 13.5,
  lineHeight: 1.6,
  color: C.accent3,
  marginBottom: 14,
};

const ghostStyle: CSSProperties = {
  padding: "8px 15px",
  borderRadius: 9,
  border: `1px solid ${C.border}`,
  background: "transparent",
  color: "var(--text-dim)",
  fontFamily: MONO,
  fontSize: 13.5,
  cursor: "pointer",
};

function primaryStyle(enabled: boolean): CSSProperties {
  return {
    width: "100%",
    padding: "11px 16px",
    borderRadius: 10,
    border: "none",
    background: enabled ? `linear-gradient(90deg, ${C.accent}, ${C.accent2})` : "var(--bg-well)",
    color: enabled ? C.onPrimary : C.textFaint,
    fontFamily: MONO,
    fontSize: 15,
    fontWeight: 600,
    cursor: enabled ? "pointer" : "default",
  };
}
