import { useEffect, useMemo, useState } from "react";
import type { CSSProperties } from "react";
import {
  deleteBehavior,
  readModel,
  saveBehavior,
  saveDeclaration,
  setAxonSource,
  setEngramShape,
} from "../api";
import { C, MONO } from "../theme";
import type {
  AxonForm,
  AxonSource,
  ComponentModel,
  EngramShape,
  Field,
  FieldSpec,
  InitError,
  ProtocolSpec,
} from "../types";
import { kindColor } from "./CanvasNode";
import { CodeEditor } from "./CodeEditor";
import { FieldInput } from "./FieldInput";
import { BehaviorCard, draftFrom, draftOf } from "./BehaviorCard";
import type { DraftBehavior } from "./BehaviorCard";
import { ProtocolPicker } from "./ProtocolPicker";

const SHAPES: { id: EngramShape; label: string; blurb: string }[] = [
  {
    id: "served-over-backend",
    label: "Served over a backend",
    blurb: "Hooks in front of working storage. Both halves - the default.",
  },
  {
    id: "served",
    label: "Served only",
    blurb: "The full hook surface, no storage. You decide where the data lives.",
  },
  {
    id: "prebuilt",
    label: "Prebuilt backend",
    blurb: "Finished storage - recall() and imprint() are real methods. No hooks to add.",
  },
];

const BACKENDS = [
  { id: "in-memory", label: "InMemoryEngram" },
  { id: "sqlite", label: "SqliteEngram" },
  { id: "postgres", label: "PostgresEngram" },
];

/**
 * What can be behind an Axon. `alias` is the sugar classmethod where the SDK
 * has one - the four OpenAI-compatible providers don't, which is why they can
 * only be written through from_source and why the form control below has to
 * know the difference rather than offering a button that can't work.
 */
/** Read-only in the editor; the choice is made in the Add panel. */
const RECEPTOR_TYPES: { id: string; label: string; blurb: string; extra: boolean }[] = [
  { id: "cli", label: "CliReceptor", blurb: "A typed command becomes a TASK - argparse and the REPL are derived from its signature.", extra: false },
  { id: "api", label: "ApiReceptor", blurb: "One HTTP endpoint serving all three dispatch modes.", extra: true },
  { id: "chat", label: "ChatReceptor", blurb: "One turn, one dispatch, plus a served page. Voice is client-side only.", extra: true },
];

const SOURCES: { id: AxonSource; label: string; blurb: string; alias: boolean }[] = [
  {
    id: "custom",
    label: "Your own function",
    blurb: "Axon(neuron_fn=...) around an async function in this module.",
    alias: false,
  },
  { id: "ollama", label: "Ollama", blurb: "A local Ollama daemon.", alias: true },
  {
    id: "huggingface",
    label: "HuggingFace",
    blurb: "TGI, vLLM, LM Studio, llama.cpp, or a hosted HF endpoint.",
    alias: true,
  },
  { id: "openai", label: "OpenAI", blurb: "The Chat Completions API.", alias: true },
  { id: "anthropic", label: "Anthropic", blurb: "The Messages API.", alias: true },
  { id: "groq", label: "Groq", blurb: "OpenAI-compatible hosted endpoint.", alias: false },
  {
    id: "openrouter",
    label: "OpenRouter",
    blurb: "OpenAI-compatible hosted endpoint.",
    alias: false,
  },
  { id: "together", label: "Together", blurb: "OpenAI-compatible hosted endpoint.", alias: false },
  { id: "mistral", label: "Mistral", blurb: "OpenAI-compatible hosted endpoint.", alias: false },
  {
    id: "mcp",
    label: "MCP server",
    blurb: "A stdio MCP server's tools, wrapped as a Neuron.",
    alias: true,
  },
];

const FORMS: { id: AxonForm; label: string; blurb: string }[] = [
  {
    id: "paired",
    label: "Axon.<source>()",
    blurb: "The sugar classmethod - shortest, and the one the docs use.",
  },
  {
    id: "from_source",
    label: "Axon.from_source()",
    blurb: "The same call with the source named - reaches every provider.",
  },
];

/**
 * The interactive half of the Code tab.
 *
 * A component module is a declaration plus a set of decorated behaviours, so
 * that's what this shows: the declaration as a config form, each behaviour as
 * its own small code box, and one button listing every protocol the node can
 * still service. Whatever the file contains that Genesis doesn't model is
 * shown at the bottom, verbatim and read-only - it's the author's, and no
 * edit here touches it.
 */
export function ComponentEditor({
  projectPath,
  file,
  onChanged,
}: {
  projectPath: string;
  file: string;
  onChanged: () => void;
}) {
  const [model, setModel] = useState<ComponentModel | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [picking, setPicking] = useState(false);

  // Declaration + backend forms, held as drafts so nothing is written until Save.
  const [fields, setFields] = useState<Field[]>([]);
  const [backendFields, setBackendFields] = useState<Field[]>([]);
  const [declBusy, setDeclBusy] = useState(false);
  const [declError, setDeclError] = useState<string | null>(null);

  // Behaviour drafts, keyed by the id they had when loaded ("new" for unsaved).
  const [drafts, setDrafts] = useState<Record<string, DraftBehavior>>({});
  const [behaviorBusy, setBehaviorBusy] = useState<string | null>(null);
  const [behaviorError, setBehaviorError] = useState<Record<string, string>>({});

  /**
   * Take a freshly-read model as the new truth, without discarding edits the
   * user hasn't saved yet.
   *
   * Every edit round-trips through the server and comes back as a whole new
   * model, so a naive adopt would reset every other form on the page - save
   * one behaviour and silently lose what you'd typed into another. "carry"
   * is the set of still-dirty drafts to re-seat on top of the new model.
   */
  function adopt(m: ComponentModel, carry: Record<string, DraftBehavior> = {}) {
    setModel(m);
    const fresh = Object.fromEntries(m.behaviors.map((b) => [b.id, draftOf(b)]));
    setDrafts({ ...fresh, ...pickExisting(carry, m) });
    setBehaviorError({});
    setError(null);
    return m;
  }

  /** Drafts that differ from what's on disk, minus the one just written. */
  function dirtyDrafts(exclude?: string): Record<string, DraftBehavior> {
    const saved = new Map((model?.behaviors ?? []).map((b) => [b.id, b]));
    const out: Record<string, DraftBehavior> = {};
    for (const [key, d] of Object.entries(drafts)) {
      if (key === exclude) continue;
      const original = saved.get(key);
      if (!original || JSON.stringify(d) !== JSON.stringify(draftOf(original))) out[key] = d;
    }
    return out;
  }

  useEffect(() => {
    let cancelled = false;
    setModel(null);
    setDrafts({});
    readModel(projectPath, file)
      .then((m) => {
        if (cancelled) return;
        adopt(m);
        setFields(m.declaration?.fields ?? []);
        setBackendFields(m.backend?.fields ?? []);
      })
      .catch((e) => !cancelled && setError((e as InitError).error || "Couldn't read that component."));
    return () => {
      cancelled = true;
    };
  }, [projectPath, file]);

  const specs = useMemo(() => {
    const out = new Map<string, ProtocolSpec>();
    for (const g of model?.catalogue?.own ?? []) for (const p of g.protocols) out.set("own:" + p.name, p);
    for (const g of model?.catalogue?.host ?? []) for (const p of g.protocols) out.set("host:" + p.name, p);
    return out;
  }, [model]);

  if (error) return <div style={{ padding: 24, color: C.accent3, fontSize: 15 }}>{error}</div>;
  if (!model) return <div style={{ padding: 24, color: C.textFaint, fontWeight: 600, fontSize: 15 }}>Reading…</div>;

  if (!model.declaration) {
    // A module that *defines* a component class rather than building one is a
    // normal, documented thing to do - it just has nothing to configure.
    const defines = model.defines ?? [];
    return (
      <div style={{ padding: 24 }}>
        <div style={noteStyle}>
          {defines.length > 0 ? (
            <>
              This module defines{" "}
              {defines.map((d, i) => (
                <span key={d.name}>
                  {i > 0 && ", "}
                  <span style={{ fontFamily: MONO, color: C.text }}>
                    class {d.name}({d.base})
                  </span>
                </span>
              ))}{" "}
              — a component type, not a component. There's no declaration to configure and no
              instance to attach behaviour to; both happen in whichever module constructs it.
              The class itself is yours to edit in your editor.
            </>
          ) : (
            <>
              Genesis can't find a component declaration in this file — nothing assigned to AXON,
              EFFECTOR or ENGRAM, and no factory returning one. It's shown read-only below.
            </>
          )}
        </div>
        <ReadOnlyChunks chunks={model.other} />
      </div>
    );
  }

  const decl = model.declaration;
  const cat = model.catalogue;
  const accent = kindColor()[decl.kind];
  const declDirty = JSON.stringify(fields) !== JSON.stringify(decl.fields);
  const backendDirty =
    !!model.backend && JSON.stringify(backendFields) !== JSON.stringify(model.backend.fields);

  const usedProtocols = new Set(model.behaviors.map((b) => b.protocol));
  const usedNames = new Set(model.behaviors.map((b) => b.fn_name));

  async function run(
    fn: () => Promise<ComponentModel>,
    onFail: (msg: string) => void,
    opts: { savedBehavior?: string; savedForm?: "declaration" | "backend" } = {},
  ) {
    const carry = dirtyDrafts(opts.savedBehavior);
    const keepDecl = opts.savedForm !== "declaration" && declDirty ? fields : null;
    const keepBackend = opts.savedForm !== "backend" && backendDirty ? backendFields : null;
    try {
      const m = adopt(await fn(), carry);
      setFields(keepDecl ?? m.declaration?.fields ?? []);
      setBackendFields(keepBackend ?? m.backend?.fields ?? []);
      onChanged();
      return true;
    } catch (e) {
      onFail((e as InitError).error || "That edit was refused.");
      return false;
    }
  }

  async function saveDecl(which: "declaration" | "backend") {
    setDeclBusy(true);
    setDeclError(null);
    await run(
      () =>
        saveDeclaration({
          path: projectPath,
          file,
          fields: which === "backend" ? backendFields : fields,
          which,
        }),
      setDeclError,
      { savedForm: which },
    );
    setDeclBusy(false);
  }

  async function changeSource(source: AxonSource, form?: AxonForm) {
    setDeclBusy(true);
    setDeclError(null);
    await run(() => setAxonSource({ path: projectPath, file, source, form }), setDeclError);
    setDeclBusy(false);
  }

  async function changeShape(shape: EngramShape, backend: string) {
    setDeclBusy(true);
    setDeclError(null);
    await run(() => setEngramShape({ path: projectPath, file, shape, backend }), setDeclError);
    setDeclBusy(false);
  }

  async function saveDraft(key: string) {
    const d = drafts[key];
    if (!d) return;
    setBehaviorBusy(key);
    setBehaviorError((e) => ({ ...e, [key]: "" }));
    const ok = await run(
      () =>
        saveBehavior({
          path: projectPath,
          file,
          behavior_id: d.behavior_id,
          scope: d.scope,
          protocol: d.protocol,
          fn_name: d.fn_name,
          signature: d.signature,
          body: d.body,
          args: d.args,
          is_async: d.is_async,
          indent: d.indent,
        }),
      (msg) => setBehaviorError((e) => ({ ...e, [key]: msg })),
      { savedBehavior: key },
    );
    setBehaviorBusy(null);
    if (ok && key === "new") setDrafts((d2) => { const { new: _drop, ...rest } = d2; return rest; });
  }

  async function removeBehavior(key: string) {
    const d = drafts[key];
    if (!d) return;
    if (!d.behavior_id) {
      setDrafts((x) => { const { [key]: _drop, ...rest } = x; return rest; });
      return;
    }
    setBehaviorBusy(key);
    await run(
      () => deleteBehavior(projectPath, file, d.behavior_id!),
      (msg) => setBehaviorError((e) => ({ ...e, [key]: msg })),
      { savedBehavior: key },
    );
    setBehaviorBusy(null);
  }

  function pick(scope: "own" | "host", spec: ProtocolSpec) {
    setDrafts((d) => ({ ...d, new: draftFrom(scope, spec, usedNames) }));
    setPicking(false);
  }

  const saved = new Map(model.behaviors.map((b) => [b.id, b]));
  const keys = [...model.behaviors.map((b) => b.id), ...(drafts.new ? ["new"] : [])];

  return (
    <div style={{ flex: 1, overflowY: "auto", padding: "18px 22px 60px" }}>
      {/* What this component is */}
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 16 }}>
        <span style={{ fontFamily: MONO, fontSize: 15, color: C.text }}>{decl.target}</span>
        <span style={badge(accent)}>{decl.kind}</span>
        <span style={{ fontFamily: MONO, fontSize: 13.5, color: C.textFaint, fontWeight: 600, }}>
          {decl.callee}() · {file}
        </span>
        {model.shape === "custom" && (
          <span
            style={badge(C.effector)}
            title={`${decl.callee} is defined in this project, so Genesis configures it but can't know which hooks it carries.`}
          >
            your class
          </span>
        )}
        {decl.scope === "factory" && (
          <span style={badge(C.effector)} title={`Built per call by ${decl.factory}().`}>
            factory
          </span>
        )}
      </div>

      {/* Neuron source - the Axon's structural choice. An Axon wraps either a
          function this project wrote or a provider the SDK builds, and the
          two take different keywords, so the form below follows this. */}
      {decl.kind === "neuron" && decl.shape !== "custom" && (
        <Card title="Source" accent={accent}>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit,minmax(200px,1fr))",
              gap: 8,
            }}
          >
            {SOURCES.map((s) => {
              const on = decl.source === s.id;
              return (
                <div
                  key={s.id}
                  onClick={() => !on && !declBusy && changeSource(s.id)}
                  style={{
                    border: `1px solid ${on ? accent + "66" : C.border}`,
                    background: on ? accent + "12" : "transparent",
                    borderRadius: 9,
                    padding: "9px 11px",
                    cursor: on || declBusy ? "default" : "pointer",
                  }}
                >
                  <div style={{ fontSize: 14, fontFamily: MONO, color: on ? accent : C.text }}>
                    {s.label}
                  </div>
                  <div
                    style={{
                      fontSize: 13,
                      color: C.textFaint, fontWeight: 600,
                      marginTop: 4,
                      lineHeight: 1.45,
                    }}
                  >
                    {s.blurb}
                  </div>
                </div>
              );
            })}
          </div>

          {/* Only shown when both forms are reachable: the four
              OpenAI-compatible providers have no classmethod of their own. */}
          {decl.source !== "custom" &&
            SOURCES.find((s) => s.id === decl.source)?.alias && (
              <div style={{ marginTop: 12, display: "flex", alignItems: "center", gap: 8 }}>
                <span style={{ fontSize: 13.5, fontFamily: MONO, color: C.textDim, fontWeight: 600, }}>written as</span>
                {FORMS.map((f) => {
                  const on = decl.form === f.id;
                  return (
                    <button
                      key={f.id}
                      title={f.blurb}
                      onClick={() => !on && !declBusy && changeSource(decl.source!, f.id)}
                      style={{
                        ...ghost,
                        color: on ? accent : C.textDim,
                        borderColor: on ? accent + "55" : C.border,
                        background: on ? accent + "12" : "transparent",
                      }}
                    >
                      {f.label}
                    </button>
                  );
                })}
              </div>
            )}

          <div
            style={{ fontSize: 13, color: C.textFaint, fontWeight: 600, marginTop: 12, lineHeight: 1.5 }}
          >
            Switching provider keeps this Axon's identity and wiring and drops the old
            provider's own keywords - a model name or endpoint means nothing to the next
            one. Nothing else in the file is touched.
          </div>
        </Card>
      )}

      {/* Receptor type. Shown the same way as the Engram shape above, but
          deliberately NOT clickable: the three classes take different
          constructor keywords and expose different decorators, so switching
          would silently drop config rather than carry it over. Picked once,
          when the module is created. */}
      {decl.kind === "receptor" && decl.shape !== "custom" && (
        <Card title="Type" accent={accent}>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(180px,1fr))", gap: 8 }}>
            {RECEPTOR_TYPES.map((t) => {
              const on = model.shape === t.id;
              return (
                <div
                  key={t.id}
                  style={{
                    border: `1px solid ${on ? accent + "66" : C.border}`,
                    background: on ? accent + "12" : "transparent",
                    borderRadius: 9,
                    padding: "9px 11px",
                    opacity: on ? 1 : 0.45,
                  }}
                >
                  <div style={{ fontSize: 14, fontFamily: MONO, color: on ? accent : C.text }}>
                    {t.label}
                  </div>
                  <div style={{ fontSize: 13, color: C.textFaint, fontWeight: 600, marginTop: 4, lineHeight: 1.45 }}>
                    {t.blurb}
                  </div>
                </div>
              );
            })}
          </div>
          <div style={{ fontSize: 13, color: C.textFaint, fontWeight: 600, marginTop: 10, lineHeight: 1.5 }}>
            Fixed once created — the three take different keywords and expose
            different hooks, so this is a rewrite rather than a toggle. Add a
            new Receptor to use a different one.
          </div>
          {RECEPTOR_TYPES.find((t) => t.id === model.shape)?.extra && (
            <div style={{ fontSize: 13, color: C.warn, marginTop: 6, lineHeight: 1.5 }}>
              Needs <code style={{ fontFamily: MONO }}>pip install 'cosmonapse[receptor]'</code> —
              FastAPI is an optional extra, so importing this module fails without it.
            </div>
          )}
        </Card>
      )}

      {/* Engram shape - the one structural choice, because the SDK splits
          storage from hooks and only one of the two has decorators. */}
      {decl.kind === "engram" && (
        <Card title="Shape" accent={accent}>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(200px,1fr))", gap: 8 }}>
            {SHAPES.map((s) => {
              const on = model.shape === s.id;
              return (
                <div
                  key={s.id}
                  onClick={() =>
                    !on && changeShape(s.id, model.backend?.backend ?? "in-memory")
                  }
                  style={{
                    border: `1px solid ${on ? accent + "66" : C.border}`,
                    background: on ? accent + "12" : "transparent",
                    borderRadius: 9,
                    padding: "9px 11px",
                    cursor: on ? "default" : "pointer",
                  }}
                >
                  <div style={{ fontSize: 14, fontFamily: MONO, color: on ? accent : C.text }}>
                    {s.label}
                  </div>
                  <div style={{ fontSize: 13, color: C.textFaint, fontWeight: 600, marginTop: 4, lineHeight: 1.45 }}>
                    {s.blurb}
                  </div>
                </div>
              );
            })}
          </div>

          {model.shape !== "served" && (
            <div style={{ marginTop: 12, display: "flex", alignItems: "center", gap: 8 }}>
              <span style={{ fontSize: 13.5, fontFamily: MONO, color: C.textDim, fontWeight: 600, }}>backend</span>
              {BACKENDS.map((b) => {
                const on = (model.backend?.backend ?? backendOf(decl.callee)) === b.id;
                return (
                  <button
                    key={b.id}
                    onClick={() => !on && changeShape(model.shape as EngramShape, b.id)}
                    style={{
                      ...ghost,
                      color: on ? accent : C.textDim,
                      borderColor: on ? accent + "55" : C.border,
                      background: on ? accent + "12" : "transparent",
                    }}
                  >
                    {b.label}
                  </button>
                );
              })}
            </div>
          )}
        </Card>
      )}

      {/* The declaration, as a form */}
      <Card
        title="Declaration"
        accent={accent}
        subtitle="Identity on the bus - published on REGISTER."
        action={
          <>
            {declDirty && (
              <button onClick={() => setFields(decl.fields)} style={ghost}>
                revert
              </button>
            )}
            <button
              onClick={() => saveDecl("declaration")}
              disabled={!declDirty || declBusy}
              style={saveBtn(declDirty && !declBusy, accent)}
            >
              {declBusy ? "saving…" : "save"}
            </button>
          </>
        }
      >
        <FormFields
          fields={fields}
          specs={cat?.declaration_fields ?? []}
          names={model.async_fns}
          onChange={setFields}
        />
        {declError && <div style={errorStyle}>{declError}</div>}
      </Card>

      {/* Delegated storage, when there is any */}
      {model.backend && (
        <Card
          title="Storage"
          accent={accent}
          subtitle={`${model.backend.callee} — the backend the handlers below forward to.`}
          action={
            <button
              onClick={() => saveDecl("backend")}
              disabled={!backendDirty || declBusy}
              style={saveBtn(backendDirty && !declBusy, accent)}
            >
              save
            </button>
          }
        >
          <FormFields
            fields={backendFields}
            specs={cat?.declaration_fields ?? []}
            names={model.async_fns}
            onChange={setBackendFields}
          />
        </Card>
      )}

      {/* Behaviours */}
      <div style={{ display: "flex", alignItems: "center", gap: 10, margin: "26px 0 12px" }}>
        <span style={{ fontSize: 13.5, fontFamily: MONO, color: C.text }}>Behaviour</span>
        <span style={{ fontSize: 13, color: C.textFaint, fontWeight: 600, }}>
          {model.behaviors.length === 0
            ? "nothing registered yet"
            : `${model.behaviors.length} decorator${model.behaviors.length === 1 ? "" : "s"}`}
        </span>
        <button
          onClick={() => setPicking((p) => !p)}
          style={{ ...ghost, marginLeft: "auto", color: accent, borderColor: accent + "55" }}
        >
          + add behaviour
        </button>
      </div>

      {picking && cat && (
        <ProtocolPicker
          catalogue={cat}
          taken={usedProtocols}
          onPick={pick}
          onClose={() => setPicking(false)}
        />
      )}

      {keys.length === 0 && !picking && (
        <div style={noteStyle}>
          This component declares an identity and nothing else yet. Add a behaviour to give it
          something to do - every protocol it can service is one click away.
        </div>
      )}

      {keys.map((key) => {
        const draft = drafts[key];
        if (!draft) return null;
        const original = saved.get(key);
        const dirty = key === "new" || !original || JSON.stringify(draft) !== JSON.stringify(draftOf(original));
        return (
          <BehaviorCard
            key={key}
            draft={draft}
            spec={specs.get(`${draft.scope}:${draft.protocol}`)}
            target={decl.target}
            dirty={dirty}
            busy={behaviorBusy === key}
            error={behaviorError[key] || null}
            onChange={(d) => setDrafts((x) => ({ ...x, [key]: d }))}
            onSave={() => saveDraft(key)}
            onRevert={() => original && setDrafts((x) => ({ ...x, [key]: draftOf(original) }))}
            onDelete={() => removeBehavior(key)}
          />
        );
      })}

      <ReadOnlyChunks chunks={model.other} />
    </div>
  );
}

function backendOf(callee: string): string {
  if (callee === "SqliteEngram") return "sqlite";
  if (callee === "PostgresEngram") return "postgres";
  return "in-memory";
}

/**
 * The declaration form: the keywords the file sets, plus a way to add the
 * ones it doesn't. A field the module omits is invisible otherwise, which
 * is how config surfaces quietly become "edit the file by hand" surfaces.
 */
function FormFields({
  fields,
  specs,
  names,
  onChange,
}: {
  fields: Field[];
  specs: FieldSpec[];
  names: string[];
  onChange: (f: Field[]) => void;
}) {
  const present = new Set(fields.map((f) => f.name));
  const missing = specs.filter((s) => !present.has(s.name));

  return (
    <>
      {fields.map((f, i) => (
        <FieldInput
          key={f.name}
          field={f}
          spec={specs.find((s) => s.name === f.name)}
          names={names}
          onChange={(next) => onChange(fields.map((x, j) => (j === i ? next : x)))}
          onRemove={() => onChange(fields.filter((_, j) => j !== i))}
        />
      ))}
      {missing.length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 4 }}>
          <span style={{ fontSize: 13, color: C.textFaint, fontWeight: 600, alignSelf: "center" }}>add:</span>
          {missing.map((s) => (
            <span
              key={s.name}
              title={s.blurb}
              onClick={() =>
                onChange([
                  ...fields,
                  {
                    name: s.name,
                    type: s.type,
                    value: s.type === "string_list" ? [] : s.type === "number" ? 0 : "",
                  },
                ])
              }
              style={addChip}
            >
              + {s.name}
            </span>
          ))}
        </div>
      )}
    </>
  );
}

/** Everything in the file Genesis doesn't model - shown, never touched. */
function ReadOnlyChunks({ chunks }: { chunks: { label: string; text: string }[] }) {
  const [open, setOpen] = useState(false);
  if (chunks.length === 0) return null;
  return (
    <div style={{ marginTop: 26 }}>
      <div
        onClick={() => setOpen((o) => !o)}
        style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer", marginBottom: 10 }}
      >
        <span style={{ fontSize: 13.5, fontFamily: MONO, color: C.textDim, fontWeight: 600, }}>
          {open ? "▾" : "▸"} Rest of the file
        </span>
        <span style={{ fontSize: 13, color: C.textFaint, fontWeight: 600, }}>
          {chunks.map((c) => c.label).join(" · ")} — read-only here, yours to edit in your editor
        </span>
      </div>
      {open &&
        chunks.map((c, i) => (
          <div key={i} style={{ marginBottom: 10 }}>
            <div style={{ fontSize: 13, color: C.textFaint, fontWeight: 600, marginBottom: 4, fontFamily: MONO }}>
              {c.label}
            </div>
            <CodeEditor value={c.text} onChange={() => {}} readOnly minRows={1} maxRows={30} />
          </div>
        ))}
    </div>
  );
}

function Card({
  title,
  subtitle,
  accent,
  action,
  children,
}: {
  title: string;
  subtitle?: string;
  accent: string;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div
      style={{
        border: `1px solid ${C.border}`,
        borderRadius: 11,
        padding: "14px 16px",
        marginBottom: 14,
        background: "rgba(var(--fg-rgb), 0.012)",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 14 }}>
        <span style={{ fontSize: 13.5, fontFamily: MONO, color: accent }}>{title}</span>
        {subtitle && <span style={{ fontSize: 13, color: C.textFaint, fontWeight: 600, }}>{subtitle}</span>}
        {action && <div style={{ marginLeft: "auto", display: "flex", gap: 7 }}>{action}</div>}
      </div>
      {children}
    </div>
  );
}

const ghost: CSSProperties = {
  background: "transparent",
  border: "1px solid var(--border)",
  borderRadius: 7,
  color: "var(--text-dim)",
  padding: "4px 10px",
  fontSize: 13,
  fontFamily: MONO,
  cursor: "pointer",
};

function saveBtn(on: boolean, accent: string): CSSProperties {
  return {
    ...ghost,
    color: on ? accent : C.textFaint,
    borderColor: on ? accent + "55" : C.border,
    background: on ? accent + "14" : "transparent",
    cursor: on ? "pointer" : "default",
  };
}

function badge(accent: string): CSSProperties {
  return {
    fontSize: 12.5,
    fontFamily: MONO,
    textTransform: "uppercase",
    letterSpacing: "0.06em",
    color: accent,
    border: `1px solid ${accent}44`,
    background: accent + "12",
    borderRadius: 999,
    padding: "2px 8px",
  };
}

const noteStyle: CSSProperties = {
  fontSize: 14,
  color: "var(--text-dim)",
  lineHeight: 1.55,
  background: "rgba(var(--fg-rgb), 0.02)",
  border: "1px solid var(--border)",
  borderRadius: 9,
  padding: "12px 14px",
  marginBottom: 14,
};

const errorStyle: CSSProperties = {
  fontSize: 13.5,
  color: "var(--accent3)",
  lineHeight: 1.5,
  background: "rgba(var(--accent3-rgb), 0.07)",
  border: "1px solid rgba(var(--accent3-rgb), 0.25)",
  borderRadius: 7,
  padding: "9px 11px",
  marginTop: 10,
};

const addChip: CSSProperties = {
  fontSize: 13,
  fontFamily: MONO,
  color: "var(--text-dim)",
  border: "1px dashed var(--border-strong)",
  borderRadius: 999,
  padding: "3px 9px",
  cursor: "pointer",
};

/** Keep only carried drafts whose behaviour still exists (or is unsaved). */
function pickExisting(
  carry: Record<string, DraftBehavior>,
  m: ComponentModel,
): Record<string, DraftBehavior> {
  const ids = new Set(m.behaviors.map((b) => b.id));
  return Object.fromEntries(
    Object.entries(carry).filter(([key]) => key === "new" || ids.has(key)),
  );
}
