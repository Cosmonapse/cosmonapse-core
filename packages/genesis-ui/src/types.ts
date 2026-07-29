export interface BrowseEntry {
  name: string;
  path: string;
}

export interface BrowseResult {
  path: string;
  parent: string | null;
  entries: BrowseEntry[];
}

export interface InitResult {
  target: string;
  written: string[];
  namespace: string;
}

export interface InitError {
  error: string;
  exists?: boolean;
}

export interface ScaffoldNode {
  id: string;
  file: string;
}

export interface ScaffoldResult {
  project: string;
  path: string;
  /** The namespace config.py was scaffolded with; null when there's none to read. */
  namespace: string | null;
  synapse: { id: string };
  neurons: ScaffoldNode[];
  effectors: ScaffoldNode[];
  engrams: ScaffoldNode[];
  /** Optional so a dist built against an older backend still typechecks -
   *  the canvas guards on it rather than trusting the key is there. */
  receptors?: ScaffoldNode[];
  /** Every source file the Code tab can open, project-relative. */
  files: string[];
}

/**
 * The four primitives a project can grow: Neurons think, Engrams remember,
 * Effectors act, Receptors listen.
 */
export type ComponentKind = "neuron" | "effector" | "engram" | "receptor";

/**
 * Which of the three Receptor classes a module is written with.
 *
 * Unlike an Engram's shape this is *not* switchable in place: the three take
 * different constructor keywords and expose different decorators, so changing
 * one into another is a rewrite rather than a toggle. It is chosen when the
 * module is created and read back from the callee afterwards.
 */
export type ReceptorShape = "cli" | "api" | "chat";

export interface ComponentResult {
  kind: ComponentKind;
  /** Receptors only: which class it was written with. Empty for other kinds. */
  shape?: string;
  id: string;
  file: string;
  path: string;
  /** False when brain.py couldn't be edited - the module was still written. */
  wired: boolean;
  note: string;
}

export interface FileResult {
  path: string;
  file: string;
  text: string;
}

// ── the interactive Code tab ──────────────────────────────────────────────
// Mirrors cosmo/commands/_genesis_ast.py's view of a component module: a
// declaration you render as a form, behaviours you render as code boxes,
// and everything Genesis doesn't model, surfaced verbatim.

/** How a declaration keyword's value is represented, and therefore edited. */
export type FieldType = "string" | "string_list" | "number" | "bool" | "none" | "name" | "expr";

export interface Field {
  name: string;
  type: FieldType;
  value: string | number | boolean | string[] | null;
}

/** Form metadata for a constructor keyword, whether or not the file sets it. */
export interface FieldSpec {
  name: string;
  type: FieldType;
  blurb: string;
  required: boolean;
  suggest: string[];
  placeholder: string;
}

export interface Declaration {
  target: string;
  callee: string;
  kind: ComponentKind;
  shape: string;
  /** "module" for a module-level assignment, "factory" when built per call. */
  scope: "module" | "factory";
  factory: string | null;
  /** Axons only: the Neuron provider behind this Axon, or "custom". */
  source?: AxonSource;
  /** Axons only: which of the three build forms wrote it. */
  form?: AxonForm;
  fields: Field[];
  lineno: number;
  end_lineno: number;
}

export interface BackendDecl {
  name: string;
  callee: string;
  backend: string;
  fields: Field[];
  lineno: number;
  end_lineno: number;
}

export interface Behavior {
  id: string;
  scope: "own" | "host";
  protocol: string;
  target: string;
  args: Record<string, Field>;
  fn_name: string;
  is_async: boolean;
  signature: string;
  body: string;
  /** False when the body couldn't be safely dedented - round-trip it verbatim. */
  dedented: boolean;
  lineno: number;
  end_lineno: number;
}

export interface ProtocolSpec {
  name: string;
  label: string;
  blurb: string;
  handler_args: string;
  body: string;
  /** `value` is the starter written when the arg is required. Booleans and
   *  string lists are real values, not strings - CliReceptor's `local` and
   *  ApiReceptor's `methods` both need to render as Python literals. */
  decorator_args: (FieldSpec & { value: string | number | boolean | string[] })[];
}

export interface ProtocolGroup {
  title: string;
  protocols: ProtocolSpec[];
}

export interface Catalogue {
  kind: ComponentKind;
  shape: string;
  declaration_fields: FieldSpec[];
  own: ProtocolGroup[];
  host: ProtocolGroup[];
  own_empty_reason: string | null;
}

/** A component class this module defines, rather than instantiates. */
export interface DefinedBase {
  name: string;
  base: string;
  kind: ComponentKind;
}

export interface ComponentModel {
  file: string;
  text: string;
  kind: ComponentKind | null;
  shape: string | null;
  /** Non-empty when the module defines a backend class (no instance to configure). */
  defines: DefinedBase[];
  declaration: Declaration | null;
  backend: BackendDecl | null;
  behaviors: Behavior[];
  async_fns: string[];
  other: { label: string; text: string; lineno: number }[];
  catalogue: Catalogue | null;
}

export type EngramShape = "prebuilt" | "served" | "served-over-backend";

/**
 * An Axon's two axes, the Neuron-side analogue of an Engram's shape+backend.
 * `source` is which provider builds the Neuron; `form` is how the pairing is
 * written, which matters because only the from_source path attaches a
 * recogniser and teaches the model the cosmo intent convention.
 */
export type AxonSource =
  | "custom"
  | "ollama"
  | "huggingface"
  | "openai"
  | "anthropic"
  | "groq"
  | "openrouter"
  | "together"
  | "mistral"
  | "mcp";

export type AxonForm = "explicit" | "paired" | "from_source";

// ── opening an existing project ───────────────────────────────────────────

export interface ProjectCounts {
  neurons: number;
  engrams: number;
  effectors: number;
  receptors?: number;
}

export interface ChildProject {
  name: string;
  path: string;
  counts: ProjectCounts;
}

/** Non-blocking notes about what Genesis won't be able to do with a project. */
export interface ImportWarning {
  id: string;
  text: string;
}

export interface Detection {
  path: string;
  name: string;
  is_project: boolean;
  /** Why it can't be opened, when is_project is false. */
  reason: string | null;
  markers: string[];
  counts: Partial<ProjectCounts>;
  warnings: ImportWarning[];
  /** True when it follows the standard skeleton (brain.py + component packages). */
  scaffolded: boolean;
  /** Projects one level down - offered when this folder itself isn't one. */
  children: ChildProject[];
}

// ── the Test tab ──────────────────────────────────────────────────────────
// Everything here is read off the source by /api/receptors, not off a running
// process. That is why the receptor list is populated before you press Run -
// and it is why a CliReceptor is drivable at all: its surface is a set of
// decorated functions, which a browser could never discover over HTTP.

/** One parameter of a @RECEPTOR.command, and what it becomes on the CLI. */
export interface CommandParam {
  name: string;
  annotation: string;
  default: string;
  required: boolean;
  /** no default -> positional, bool default -> switch, else a --flag. */
  form: "positional" | "flag" | "switch";
}

export interface ReceptorCommand {
  name: string;
  help: string;
  /** Answered in the receptor without dispatching. */
  local: boolean;
  is_default: boolean;
  fn_name: string;
  params: CommandParam[];
}

export interface ReceptorInfo {
  id: string;
  file: string;
  /** Project-relative, e.g. "receptors/terminal.py". */
  path: string;
  shape: ReceptorShape | "custom" | "";
  callee: string;
  /** api/chat only - they need cosmonapse[receptor]. */
  needs_extra: boolean;
  neuron: string;
  capabilities: string[];
  /** Declared keywords, with the SDK default filled in where unset - what
   *  the receptor will actually use, rather than what the file happens to say. */
  config: Record<string, string | number | boolean | string[] | null>;
  /** CliReceptor only. */
  commands: ReceptorCommand[];
}

export interface ReceptorList {
  path: string;
  has_brain: boolean;
  receptors: ReceptorInfo[];
}

/** The project's brain.py process. One per project, started explicitly. */
export interface BrainStatus {
  running: boolean;
  path: string;
  pid: number | null;
  exit_code: number | null;
  started_at: number | null;
  uptime_s: number | null;
  stopped?: boolean;
}

/** A reply relayed by /api/receptor/http. `ok` is about the transport, not
 *  the status code - a 500 that arrived is ok: true. */
export interface ProxyResult {
  ok: boolean;
  url: string;
  status?: number;
  content_type?: string;
  elapsed_ms: number;
  text?: string;
  json?: unknown;
  error?: string;
}

export interface RecentProject {
  path: string;
  name: string;
}

// ── the synapse a project talks to ─────────────────────────────────────
// Genesis never hosts the synapse - it probes one, spawns one, and points
// Prism at it. Every status is a fresh probe, so a synapse started from a
// terminal reads the same as one started from this UI.

/** The transports the synapse form offers. Only "dev" can be started here. */
export type SynapseTransport = "memory" | "dev" | "nats" | "kafka";

export interface SynapseStatus {
  live: boolean;
  url: string;
  namespace: string;
  transport: string | null;
  signal_count: number | null;
  client_count: number | null;
  started_at: string | null;
  /** Why it isn't live, in words the indicator can show as-is. */
  reason: string | null;
  /** True when this Genesis spawned the process behind it. */
  managed: boolean;
  /** Only set on the reply to a stop. */
  stopped?: boolean;
}

export interface PrismLaunch {
  /** The tab to open: Prism's SPA, pre-pointed at url + namespace. */
  url: string;
  port: number;
  started: boolean;
  reused: boolean;
  namespace: string;
  synapse_url: string;
}
