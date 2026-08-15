import type {
  ArchivedList,
  AxonForm,
  AxonSource,
  BrowseResult,
  PrismLaunch,
  ComponentKind,
  ComponentModel,
  ComponentResult,
  Detection,
  EngramShape,
  Field,
  FileResult,
  ForgeAccount,
  ForgeKind,
  ForgeRepoList,
  GitBranches,
  GitCommitDetail,
  GitDiff,
  GitLog,
  GitStatus,
  InitError,
  BrainStatus,
  InitResult,
  ProxyResult,
  ReceptorList,
  ReceptorShape,
  RemoveMode,
  RemoveResult,
  RestoreResult,
  ScaffoldResult,
  SynapseStatus,
} from "./types";

// Thin fetch wrapper over the local API cosmo/commands/_genesis.py exposes.
// Kept as one small module (mirroring prism-ui's useSignalStream.ts being
// the sole point of contact with its server) so the rest of the app never
// touches `fetch` directly.

async function asJson<T>(res: Response): Promise<T> {
  const body = await res.json();
  if (!res.ok) {
    throw body as InitError;
  }
  return body as T;
}

/** POST JSON, unwrap JSON. Added when the Test tab tripled the number of
 *  posting endpoints and the inline fetch boilerplate stopped earning its
 *  place; the older calls below still spell it out. */
function post<T>(url: string, body: unknown): Promise<T> {
  return fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).then((r) => asJson<T>(r));
}

export function browse(path?: string): Promise<BrowseResult> {
  const qs = path ? `?path=${encodeURIComponent(path)}` : "";
  return fetch(`/api/browse${qs}`).then((r) => asJson<BrowseResult>(r));
}

export function initProject(args: {
  name: string;
  path: string;
  namespace: string;
  force?: boolean;
  /** Start a repository in the new project. Defaults to true server-side; a
   *  failure lands on the result, never as a failed scaffold. */
  git?: boolean;
}): Promise<InitResult> {
  return fetch("/api/init", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(args),
  }).then((r) => asJson<InitResult>(r));
}

export function readScaffold(path: string): Promise<ScaffoldResult> {
  return fetch(`/api/scaffold?path=${encodeURIComponent(path)}`).then((r) =>
    asJson<ScaffoldResult>(r),
  );
}

/** Write a new component module and wire it into brain.py. */
export function addComponent(args: {
  path: string;
  kind: ComponentKind;
  name: string;
  /** Receptors only - picks which of the three classes to write. */
  shape?: ReceptorShape;
  force?: boolean;
}): Promise<ComponentResult> {
  return fetch("/api/component", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(args),
  }).then((r) => asJson<ComponentResult>(r));
}

/**
 * Unwire a component from brain.py, then archive or delete its module.
 *
 * One call for both modes: the difference is a single verb at the end of a
 * journey that is otherwise identical, and two endpoints would be two ways
 * for the unwiring to drift apart.
 */
export function removeComponent(args: {
  path: string;
  /** Project-relative: "neurons/hello.py", or an "_archive/..." path to
   *  delete something already archived. */
  file: string;
  mode: RemoveMode;
}): Promise<RemoveResult> {
  return post<RemoveResult>("/api/component/delete", args);
}

/** Move an archived module back where it came from and re-wire brain.py. */
export function restoreComponent(path: string, file: string): Promise<RestoreResult> {
  return post<RestoreResult>("/api/component/restore", { path, file });
}

/** What is in this project's _archive/, newest first. */
export function readArchived(path: string): Promise<ArchivedList> {
  return fetch(`/api/archived?path=${encodeURIComponent(path)}`).then((r) =>
    asJson<ArchivedList>(r),
  );
}

/** Read one project file back as text (the Code tab). */
export function readFile(path: string, file: string): Promise<FileResult> {
  return fetch(
    `/api/file?path=${encodeURIComponent(path)}&file=${encodeURIComponent(file)}`,
  ).then((r) => asJson<FileResult>(r));
}

// ── the interactive Code tab ──────────────────────────────────────────────
// Every structured edit posts {path, file, ...} and gets the *re-read*
// component model back, so the UI never has to guess what the file now looks
// like - the server is the only thing that decides that.

export function writeFile(path: string, file: string, text: string): Promise<FileResult> {
  return fetch("/api/file", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, file, text }),
  }).then((r) => asJson<FileResult>(r));
}

/** Create helpers.py if this project doesn't have one yet. */
export function ensureHelpers(path: string): Promise<{ file: string; created: boolean; text: string }> {
  return fetch("/api/helpers", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  }).then((r) => asJson<{ file: string; created: boolean; text: string }>(r));
}

export function readModel(path: string, file: string): Promise<ComponentModel> {
  return fetch(
    `/api/model?path=${encodeURIComponent(path)}&file=${encodeURIComponent(file)}`,
  ).then((r) => asJson<ComponentModel>(r));
}

function edit<T>(route: string, body: unknown): Promise<T> {
  return fetch(route, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).then((r) => asJson<T>(r));
}

export function saveDeclaration(args: {
  path: string;
  file: string;
  fields: Field[];
  which?: "declaration" | "backend";
}): Promise<ComponentModel> {
  return edit<ComponentModel>("/api/declaration", args);
}

export function saveBehavior(args: {
  path: string;
  file: string;
  behavior_id?: string | null;
  scope: "own" | "host";
  protocol: string;
  fn_name: string;
  signature: string;
  body: string;
  args?: Field[];
  is_async?: boolean;
  indent?: boolean;
}): Promise<ComponentModel> {
  return edit<ComponentModel>("/api/behavior", args);
}

export function deleteBehavior(
  path: string,
  file: string,
  behavior_id: string,
): Promise<ComponentModel> {
  return edit<ComponentModel>("/api/behavior/delete", { path, file, behavior_id });
}

/** Write the Neuron's system prompt constant, adding it if there isn't one. */
export function savePrompt(args: {
  path: string;
  file: string;
  prompt: string;
  name?: string;
}): Promise<ComponentModel> {
  return edit<ComponentModel>("/api/prompt", args);
}

export function setEngramShape(args: {
  path: string;
  file: string;
  shape: EngramShape;
  backend?: string;
}): Promise<ComponentModel> {
  return edit<ComponentModel>("/api/engram-shape", args);
}

/** Repoint an Axon at another Neuron source, or rewrite it in another form. */
export function setAxonSource(args: {
  path: string;
  file: string;
  source: AxonSource;
  form?: AxonForm;
}): Promise<ComponentModel> {
  return edit<ComponentModel>("/api/axon-source", args);
}

/** Can this folder be opened as a project, and what will opening it show? */
export function detect(path: string): Promise<Detection> {
  return fetch(`/api/detect?path=${encodeURIComponent(path)}`).then((r) =>
    asJson<Detection>(r),
  );
}

// ── the synapse ────────────────────────────────────────────────────

/** Is a synapse serving this namespace at this URL right now? */
export function synapseStatus(url: string, namespace: string): Promise<SynapseStatus> {
  return fetch(
    `/api/synapse?url=${encodeURIComponent(url)}&namespace=${encodeURIComponent(namespace)}`,
  ).then((r) => asJson<SynapseStatus>(r));
}

/**
 * Spawn a dev synapse on `port` and wait until it answers for `namespace`.
 * Resolves only once the namespace is actually registered, so the caller
 * never has to walk back a live indicator it showed too early.
 */
export function startSynapse(args: {
  namespace: string;
  port: number;
  host?: string;
  transport?: "dev";
}): Promise<SynapseStatus> {
  return edit<SynapseStatus>("/api/synapse/start", args);
}

export function stopSynapse(url: string, namespace: string): Promise<SynapseStatus> {
  return edit<SynapseStatus>("/api/synapse/stop", { url, namespace });
}

/** Open Prism on a live synapse, starting a Prism server if none is there. */
export function launchPrism(args: {
  url: string;
  namespace: string;
  port: number;
}): Promise<PrismLaunch> {
  return edit<PrismLaunch>("/api/prism", args);
}

// ── the Test tab ──────────────────────────────────────────────────────────

/** What this project mounts, read off the source - no process needed. */
export function readReceptors(path: string): Promise<ReceptorList> {
  return fetch(`/api/receptors?path=${encodeURIComponent(path)}`).then((r) =>
    asJson<ReceptorList>(r),
  );
}

export function brainStatus(path: string): Promise<BrainStatus> {
  return fetch(`/api/brain?path=${encodeURIComponent(path)}`).then((r) =>
    asJson<BrainStatus>(r),
  );
}

export function startBrain(path: string): Promise<BrainStatus> {
  return post<BrainStatus>("/api/brain/start", { path });
}

export function stopBrain(path: string): Promise<BrainStatus> {
  return post<BrainStatus>("/api/brain/stop", { path });
}

/**
 * Call an HTTP Receptor through Genesis rather than from the tab.
 *
 * An ApiReceptor sends no access-control-allow-origin, so a direct fetch from
 * the Genesis origin to :8000 is blocked before it leaves the browser. Going
 * through the server that served the page sidesteps CORS, and means transport
 * failures arrive as readable errors instead of an opaque TypeError.
 */
export function receptorHttp(args: {
  path: string;
  file: string;
  method?: string;
  endpoint?: string;
  body?: unknown;
  timeout_s?: number;
}): Promise<ProxyResult> {
  return post<ProxyResult>("/api/receptor/http", args);
}

/** The brain's stdin/stdout, for the terminal panel. */
export function brainSocketUrl(path: string): string {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}/api/brain/ws?path=${encodeURIComponent(path)}`;
}

// ── version control ───────────────────────────────────────────────────────
// Every call returns the *re-read* status, for the same reason the structured
// edits return the re-read component model: the server is the only thing that
// decides what the repository now looks like, and a UI patching its own copy
// would drift the moment the user also did something in a terminal.

/** Branch, HEAD and the working tree, in one round trip. */
export function gitStatus(path: string): Promise<GitStatus> {
  return fetch(`/api/git?path=${encodeURIComponent(path)}`).then((r) =>
    asJson<GitStatus>(r),
  );
}

/** `git init` in the project, plus a .gitignore and a first commit. */
export function gitInit(args: {
  path: string;
  initial_commit?: boolean;
  gitignore?: boolean;
}): Promise<GitStatus> {
  return post<GitStatus>("/api/git/init", args);
}

/** Set user.name / user.email for *this repository* - never --global. */
export function gitIdentity(path: string, name: string, email: string): Promise<GitStatus> {
  return post<GitStatus>("/api/git/identity", { path, name, email });
}

/** Add paths to the index, or take them back out of it. */
export function gitStage(path: string, files: string[], staged: boolean): Promise<GitStatus> {
  return post<GitStatus>("/api/git/stage", { path, files, staged });
}

/**
 * Commit the index. `stage_all` is the one-click checkpoint - it stages
 * everything in the repository first, and the button that sends it says so
 * rather than hiding the difference.
 */
export function gitCommit(args: {
  path: string;
  message: string;
  stage_all?: boolean;
}): Promise<GitStatus> {
  return post<GitStatus>("/api/git/commit", args);
}

export function gitLog(
  path: string,
  opts: { limit?: number; file?: string } = {},
): Promise<GitLog> {
  const qs = new URLSearchParams({ path });
  if (opts.limit) qs.set("limit", String(opts.limit));
  if (opts.file) qs.set("file", opts.file);
  return fetch(`/api/git/log?${qs}`).then((r) => asJson<GitLog>(r));
}

/** One commit: header, what it touched, and the diff. */
export function gitShow(path: string, sha: string): Promise<GitCommitDetail> {
  return fetch(
    `/api/git/show?path=${encodeURIComponent(path)}&sha=${encodeURIComponent(sha)}`,
  ).then((r) => asJson<GitCommitDetail>(r));
}

/** One file's change: against the index, against HEAD, or inside a commit. */
export function gitDiff(args: {
  path: string;
  file: string;
  staged?: boolean;
  sha?: string;
}): Promise<GitDiff> {
  const qs = new URLSearchParams({ path: args.path, file: args.file });
  if (args.staged) qs.set("staged", "1");
  if (args.sha) qs.set("sha", args.sha);
  return fetch(`/api/git/diff?${qs}`).then((r) => asJson<GitDiff>(r));
}

/**
 * Put one file back - to the last commit, or to a chosen one.
 *
 * Always exactly one path, because this is the end of the API that
 * overwrites uncommitted work. The guarantee that makes it safe behind a
 * button is that it can only ever touch the file named on that button.
 */
export function gitRestore(path: string, file: string, sha?: string): Promise<GitStatus> {
  return post<GitStatus>("/api/git/restore", { path, file, sha });
}

/** Local branches, and which one is checked out. */
export function gitBranches(path: string): Promise<GitBranches> {
  return fetch(`/api/git/branches?path=${encodeURIComponent(path)}`).then((r) =>
    asJson<GitBranches>(r),
  );
}

/**
 * Check out a branch, or create one from where you are.
 *
 * The two differ in how they treat uncommitted work, and deliberately so:
 * creating carries it across, switching to an existing branch is refused
 * while the tree is dirty. The server owns that rule; this just sends the
 * flag.
 */
export function gitBranch(path: string, name: string, create = false): Promise<GitStatus> {
  return post<GitStatus>("/api/git/branch", { path, name, create });
}

/** Point this repository at a remote, adding or replacing it. */
export function gitRemote(path: string, url: string, name = "origin"): Promise<GitStatus> {
  return post<GitStatus>("/api/git/remote", { path, url, name });
}

/** Clone into `path/name`. Never into `path` itself. */
export function gitClone(args: {
  path: string;
  url: string;
  name?: string;
}): Promise<GitStatus> {
  return post<GitStatus>("/api/git/clone", args);
}

export function gitPush(path: string): Promise<GitStatus> {
  return post<GitStatus>("/api/git/push", { path });
}

/** Fast-forward onto the remote, or come back with why it can't. */
export function gitPull(path: string): Promise<GitStatus> {
  return post<GitStatus>("/api/git/pull", { path });
}

// ── the git account ───────────────────────────────────────────────────────

export function forgeStatus(): Promise<ForgeAccount> {
  return fetch("/api/forge").then((r) => asJson<ForgeAccount>(r));
}

/**
 * Hand a token to git's credential helper, after checking it works.
 *
 * The token goes over localhost to the Genesis server and from there into
 * `git credential approve`. It is never written to a Genesis file and never
 * comes back out of any endpoint - see _genesis_forge.py's docstring.
 */
export function forgeConnect(args: {
  kind: ForgeKind;
  token: string;
  base_url?: string;
  login?: string;
  /** Turn on git's plaintext `store` helper, for machines with no keyring. */
  enable_store?: boolean;
}): Promise<ForgeAccount> {
  return post<ForgeAccount>("/api/forge/connect", args);
}

export function forgeDisconnect(): Promise<ForgeAccount> {
  return post<ForgeAccount>("/api/forge/disconnect", {});
}

export function forgeRepos(q = ""): Promise<ForgeRepoList> {
  const qs = q ? `?q=${encodeURIComponent(q)}` : "";
  return fetch(`/api/forge/repos${qs}`).then((r) => asJson<ForgeRepoList>(r));
}
