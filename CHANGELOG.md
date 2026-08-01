# Changelog

All notable changes to Cosmonapse are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Removed
- **The TypeScript SDK (`@cosmonapse/sdk`) has been retired.** `packages/ts-sdk`
  is gone from the tree, along with its CI job and the npm publish step in the
  release workflow. Python is now the single reference implementation of the
  protocol. The last published state is preserved on the `archive/ts-sdk`
  branch. Nothing about the wire format changed - the envelope spec is still
  language-agnostic, and a third-party implementation in any language remains
  a first-class participant on the bus. The `cosmo` CLI is unaffected: it
  always had exactly one implementation, shipped in the Python package, and
  `pip install cosmonapse` is now the only way to get it. `/docs/typescript/*`
  on the site 308-redirects to the matching Python reference section.

### Changed
- **`cosmo doppler` is now `cosmo prism`.** The command is named after the
  thing it opens: `cosmo prism` launches the live browser visualization by
  default (what used to be `cosmo doppler --prism`), and `cosmo prism --tail`
  streams Signals to stdout (what used to be bare `cosmo doppler`). All other
  flags - `--url`, `--namespace`, `--synapse`, `--port`, `--type`, `--trace`,
  `--neuron`, `--json`, `--payload` - are unchanged. `cosmo doppler` still
  works as a hidden deprecated alias that prints a warning and forwards to
  `cosmo prism`; `--prism` is accepted and ignored. The alias will be removed
  in a future release. "Doppler" remains the protocol-level term for a
  subscriber with no queue_group (see `Synapse.subscribe`).

## [0.1.9] - 2026-07-28

Adds the sixth primitive - **Effector**, the action layer - and closes the
open-model tool-call gap 0.1.8 shipped as a known limitation. Additive
throughout: no public API was removed or renamed, and 0.1.8 code runs unchanged.
The one wire-visible change is a third `payload.role` value (see *Changed*).

### Added
- **`Effector` - the action primitive (python-sdk + ts-sdk).** The synapse-side
  participant that services TOOL_CALL signals, the way an Engram services
  RECALL / IMPRINT. Neurons think, Engrams remember, Effectors act. Addressed
  by `effector_id` (explicit) or `effector_kind` (typed); one Effector per tool
  family (filesystem, shell, websearch, fetch) is the intended deployment.
  Mounted with `dendrite.attach_effector(fx)` / `detach_effector(id)`; the
  hosting Dendrite emits REGISTER with `role="effector"` so peers and Prism see
  the tool layer directly. Two ways to write one: `Effector.serve(...)` +
  `@fx.on_tool_call` (the return value is emitted as the TOOL_RESULT) for the
  common case, or subclass `Effector` when a backend needs real lifecycle - the
  standard `@on_connect` / `@on_refresh` / `@on_schedule` trio is available on
  both. `@fx.host.on_<signal>` provides the deferred host-decorator pattern
  `Axon.host` established in 0.1.8.
  Effectors are **not** Neurons: they never produce AGENT_OUTPUT, and a failed
  invocation surfaces as `error` on the TOOL_RESULT rather than an ERROR
  signal, so a failing tool does not terminate the containing TASK. Tool calls
  inherit the containing TASK's `trace_id` and the `parent_id` chain proves
  causation. Error hierarchy: `EffectorTimeout` (deadline elapsed unanswered),
  `EffectorCancelled` (containing TASK terminated mid-call), `EffectorNotBound`
  (Neuron asked for an unwired binding), `EffectorOverloaded` (backend shedding
  load). New exports: `Effector`, `EffectorBinding`, `EffectorClient`,
  `ToolOutcome`, `EffectorError` and the four subclasses.
- **`EffectorClient` - caller-side tool I/O (python-sdk + ts-sdk).** The
  action-side twin of `EngramClient`, and a thin wrapper over a per-operation
  Pathway: correlation, buffering, deadline, and cancellation stay in the
  Pathway / Dendrite, and only the Dendrite touches the Synapse. Surfaced as
  `await dendrite.call_tool(effector_id=/effector_kind=, tool=, args=,
  deadline_ms=, ...) -> ToolOutcome`. A deadline maps to `EffectorTimeout`; a
  Pathway closed by the parent TASK's terminal event (or Dendrite shutdown)
  maps to `EffectorCancelled`.
- **`TOOL_STANDARDS` - native tool-call dialect parsers (python-sdk + ts-sdk).**
  Teaching a hosted model a bespoke `{"tool", "args"}` convention invites
  drift; speaking its mother tongue does not. Three parsers ship: `hermes`
  (Nous/Hermes `<tool_call>` XML tags - the de-facto open-model dialect used by
  Qwen, Hermes, and many fine-tunes), `claude` (Anthropic `tool_use` content
  block as JSON in text), and `codex` (OpenAI function-calling JSON - a
  `tool_calls` array, legacy `function_call`, a bare `{"name", "arguments"}`
  object, Meta's documented `{"name", "parameters"}` Llama reply shape, and the
  Responses-API schema-echo wrapper; string-encoded `arguments` are decoded).
  Each parser is pure and synchronous and returns the normalised
  `{"tool", "args", "call_id"}` on a match or `None` to fall through, so
  ordinary prose and ordinary JSON output never misfire. Where a reply carries
  several calls the first is taken - the one-action-per-step contract is the
  Axon's to enforce, not the parser's. The model never learns Cosmonapse
  exists.
- **`Axon(tool_standard=..., effectors=[...])` - declarative tool binding
  (python-sdk + ts-sdk).** An Axon declares the dialect its Neuron speaks and
  the `EffectorBinding`s it may act through; the Axon then dispatches through
  the `EffectorClient` itself. A Neuron whose signature accepts `call_tool`
  gets a bound helper injected. Validated at construction: `effectors=`
  requires `tool_standard=`, and duplicate binding names raise.
- **`Engram.serve()` - protocol-hook Engrams (python-sdk + ts-sdk).** The
  memory-side twin of `Effector.serve()`: build an Engram from the two hooks
  that matter instead of subclassing. A RECALL arrives, `@on_recall` runs, and
  its return value is published as the RECALLED hits; an IMPRINT arrives,
  `@on_imprint` runs, and its return value becomes the IMPRINTED receipt.
  `@ENGRAM.serves` optionally gates which queries this Engram answers at all.
  Subclassing `Engram` is unchanged and still right where real lifecycle is
  needed.
- **`Engram.host` - deferred Dendrite decorators for the memory side
  (python-sdk + ts-sdk).** Matches `Axon.host` and `Effector.host`:
  `@ENGRAM.host.on_imprint_signal` and friends queue at module level and are
  replayed onto the hosting Dendrite when it connects the Engram, subscription
  ensured. Distinct from `@on_recall` / `@on_imprint`, which *are* the
  servicing - a host observer does not disable them.
- **Prism metrics (`Metrics` panel).** Three timing views derived purely from
  timestamps and lineage already in the rolling signal buffer - the SDK emits
  nothing extra: time per task (TASK to last signal on the trace, or FINAL),
  tool-call latency (TOOL_CALL to matching TOOL_RESULT), and memory retrieval
  (RECALL to matching RECALLED), each with count / total / average / max.
  Pairing prefers explicit lineage (`reply.parent_id === request.id`) and falls
  back to the nearest earlier unmatched request on the same trace.
- **Prism Constellation view, signal-tree rendering, and signal grouping.** New
  `Constellation.tsx`, `SignalTree.tsx`, `SignalList.tsx`, plus `constellation.ts`
  and `grouping.ts`.
- **`neuron=` attribution on `emit_final` / `emit_error` (python-sdk +
  ts-sdk).** The reply emit helpers now take the same `neuron=` override
  the cognition helpers (`emit_plan`, `emit_tool_call`, ...) always had:
  `emit_final(..., neuron="assistant")` attributes the FINAL to the
  producing Neuron instead of the emitting Dendrite - matching
  terminal-handler promotion, so observers (doppler, Prism) keep the
  lineage TASK -> AGENT_OUTPUT -> FINAL on one participant. Default
  (omitted) is unchanged: the Dendrite's id.
- **Raw `/v1/completions` support on the `huggingface` Neuron source
  (python-sdk).** `Neuron(source="huggingface", use_completions_api=True,
  stop=[...])` posts to the OpenAI-compatible `/v1/completions` path - for
  vLLM / llama.cpp / proxy deployments that expose no chat route. The path
  takes a rendered `prompt` string ONLY: the Neuron will not guess a
  model's chat template - render it caller-side (ChatML, Llama, ...) and
  pass `prompt=`; `messages` input raises. `stop=` is also forwarded on
  the chat and TGI `/generate` paths. `use_chat_api` and
  `use_completions_api` are mutually exclusive.

### Changed
- **REGISTER's `payload.role` gained a third value, `"effector"`.** It was
  `"neuron"` or `"engram"`. Anything branching exhaustively on the universal
  discriminator - a third-party registry store, a custom observer, a non-Python
  implementation - needs a case for it or a tolerant default; treat `role` as
  an open enum. First-party consumers (registry stores, Prism) handle it.
  `design/ENVELOPE_SPEC.md` still documents only two values and catches up next
  release; the wire behaviour above is correct.
- **`cosmo init` scaffolds a tool layer.** New projects get an `effector/`
  package alongside `neurons/`, containing a working `Effector.serve()` module
  and one tool call wired end to end. The division the scaffold teaches is
  unchanged: behaviour in `neurons/` and `effector/`, deployment in `brain.py`.
- **`ruff` pinned to `0.16.0` with an explicit `[tool.ruff.lint] select` list.**
  Recent ruff versions enable ~400 rules by default, so an unpinned range made
  `ruff check` non-reproducible across dev machines and CI and turned every
  upgrade into a wall of new "errors". The committed set is the one this
  codebase is actually written against. Contributors: results will differ from
  an unpinned local install. Bump deliberately, not implicitly.
- Typing and idiom modernisation across the SDK under the new rule set
  (`datetime.UTC`, PEP 604/585 annotations, `Self` on `__aenter__`); no
  behaviour change.
- npm package version aligned to `0.1.8` (`package.json` was left at
  `0.1.7` when the 0.1.8 release was tagged).

### Removed
- The GitLab CI config (`.gitlab-ci.yml`). GitHub Actions (`ci.yml` /
  `release.yml`) is the single CI going forward.

### Known limitations
- **Effector deployment patterns are young.** The primitive and its client
  carry 49 new tests, but the one-Effector-per-tool-family guidance comes from
  design rather than production mileage; the ergonomics around `effector_kind`
  routing and multi-Effector fan-out may still move.
- **The three tool standards cover the common cases, not all of them.** A model
  emitting none of `hermes` / `claude` / `codex` falls through to `None` and
  the Neuron sees plain text. Adding a dialect is a pure function, not a
  framework change - but there is still no Axon-level reject-and-repair
  contract (`InvalidOutput(reason, hint)` + bounded `output_retries`), which
  remains 0.2.0 scope.

## [0.1.8] - 2026-07-13

The first public release of Cosmonapse. Earlier 0.1.x entries below were
internal milestones on the way here.

### Added
- **`Axon.host` - deferred Dendrite decorators (python-sdk + ts-sdk).** The
  standard way to declare host-side behaviour in a Neuron's module. Python:
  `@AXON.host.on_agent_output(neuron=...)` / `@AXON.host.on_tool_call(...)`
  queue a handler at module level; the Axon applies it to the **hosting
  Dendrite** right after that Dendrite emits REGISTER for it (before
  `@axon.on_connect` hooks fire) and ensures the inbound subscription.
  Replaces the hand-written `on_connect -> node = a.dendrite -> @node.on_* ->
  ensure_subscribed` wiring; any `Dendrite.on_*` decorator with the standard
  `(fn, *, neuron=, capability=, trace_id=)` shape is supported, names are
  validated eagerly at import time. TypeScript: `axon.host.onToolCall(fn,
  { neuron })` and the full cognition/reply `on*` family (plus the generic
  `axon.host.onSignal(type, fn, filter)`), replayed by the hosting Dendrite
  in `start()` and `addAxon()`; `AxonHost` is exported from the package
  root.
- **`cosmo` for npm users - one CLI build, two registries.** The npm package
  now installs a `cosmo` command (`npm install -g @cosmonapse/sdk`). It is a
  self-bootstrapping launcher (`bin/cosmo.js`, zero dependencies): it
  delegates to `$COSMO_PYTHON` or any system Python that already has the
  `cosmonapse` package; failing that it creates a private venv under
  `~/.cosmonapse/cli-venv` and pip-installs `cosmonapse` pinned to the npm
  package's own version (one-time, refreshed on version change). Every path
  runs `python -m cosmo`, so pip and npm users always run the single
  canonical CLI implementation from the Python package - by design there is
  no second CLI codebase to drift. Added `cosmo/__main__.py` as the
  delegation target. Requires a Python 3.11+ interpreter on PATH; without
  one the launcher exits 127 with clear instructions.

### Changed
- **`cosmo init` scaffolds the standard package skeleton.** New projects get
  `config.py` + `neurons/hello.py` + `brain.py` (wiring) + `demo.py` +
  README (entries stay thin; behaviour lives in neurons/, deployment in
  brain.py).
  `python demo.py` is self-contained: it hosts BOTH sides, and SYNAPSE_URL
  only swaps the transport (in-process MemorySynapse vs a running synapse).
  No worker.py is generated - the README carries the 10-line entry to add
  when workers should become their own processes. Replaces the old
  two-file `worker.py` + `orchestrator.py` scaffold.

### Known limitations
- **Open models drift from strict action schemas.** A harness that asks a
  plain-chat LLM for `{"tool", "args"}` JSON will eventually get shorthand
  variants or truncated objects back; today `@detects_output` can only
  transform or error, so every harness must carry its own fallback parsing. The planned fix is an Axon-level reject-and-repair contract
  (`InvalidOutput(reason, hint)` + bounded `output_retries`, plus
  `response_format`/`tools` passthrough on OpenAI-compatible providers) -
  scheduled for 0.2.0; see the roadmap.

## [0.1.6] - 2026-06-22

### Fixed
- **`on_task_offer(capability=...)` / `onTaskOffer(.., {capability})` silently
  dropped every offer.** The handler's capability filter resolved the offer's
  *directed neuron*, but a TASK_OFFER is a broadcast that carries its required
  capabilities in `payload.capabilities` and has no directed neuron  -  so the
  filter always failed and no BID was ever emitted (competitive bidding
  simply timed out). The filter now narrows against the offer's requested capability
  set; an offer with no capabilities stays open to all. Fixed at parity in both
  SDKs.

## [0.1.1] - 2026-06-05

### Added
- **New LLM provider Neurons** in both SDKs' `Neuron(source=...)` / `neuron(source, opts)` factory. First-class `"openai"` (Chat Completions) and `"anthropic"` (Messages API) sources, each a dedicated provider class (`httpx` in Python, the runtime `fetch` in TypeScript  -  no provider SDK dependency) resolving credentials from an explicit key or the `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` env vars. Plus OpenAI-compatible hosted aliases  -  `"groq"`, `"openrouter"`, `"together"`, and `"mistral"`  -  pre-configured on the existing HuggingFace neuron. All are soft dependencies (lazy-imported) and return the standard `{"response": ..., "meta": ...}` shape. The TypeScript SDK ships these at parity with the Python reference (`packages/ts-sdk/src/neuron-openai.ts`).

### Removed
- **The HTTP-app Neuron type** (`Neuron(source="flask" | "wsgi" | "api")` and
  the module `cosmonapse._neuron_http`; the TypeScript `expressNeuron` and the
  `neuron("express" | "http" | "api", …)` factory sources). An HTTP API is not
  a Neuron  -  a web app is an inbound request handler, not an `input -> output`
  worker. The supported pattern is the reverse: keep your web framework (Flask,
  Express, …) on the outside as an HTTP boundary and dispatch TASK Signals from
  its route handlers via an orchestrator Dendrite, wiring
  `@dendrite.on_agent_output` directly in the app.
- The `[flask]` optional dependency was dropped from the Python SDK; the shared
  `CloseableNeuronFn` type moved from `neuron-express.ts` to `neuron.ts` in the
  TS SDK.

## [0.1.0] - 2026-05-30

First feature release. Adds shared memory (Engram), per-trace event handles
(Pathway), the full cognition signal family, capability-routed dispatch with
competitive bidding, and a richer `cosmo` CLI. The TypeScript SDK ships its
first published version alongside the Python SDK  -  parity gaps relative to
Python are tracked in `packages/ts-sdk/PORTING_STATUS.md`.

### Added
- **Engram**  -  shared memory subsystem for Neurons. New `cosmonapse.engram`
  package with the `Engram` ABC, `EngramBinding` for declarative wiring on an
  Axon, `EngramClient` for in-Neuron access, and three backends:
  `InMemoryEngram`, `SqliteEngram`, and `PostgresEngram` (lazy-imports
  `asyncpg`). New wire types `RECALL` / `RECALLED` / `IMPRINT` / `IMPRINTED`
  with matching signal builders (`recall_signal`, `recalled_signal`,
  `imprint_signal`, `imprinted_signal`) and `new_engram_id()` ULID helper.
  Errors: `EngramTimeout`, `EngramCancelled`, `EngramNotBound`,
  `EngramOverloaded`. See `design/ENGRAM_DESIGN.md`.
- **Pathway**  -  `cosmonapse.pathway` exposes `Pathway` and `PathwayClosedError`.
  `Dendrite.dispatch(...)` and `observe_pathway(trace_id)` return a per-trace
  event handle supporting three consumption shapes on one primitive:
  `await pw.wait()`, `@pw.on(SignalType.X)`, and `async for sig in pw`.
  `Pathway(scope="all" | "terminal")` filters which signal types are delivered;
  pathways auto-close on FINAL / ERROR.
- **Cognition signal family**  -  `PLAN`, `THOUGHT_DELTA`, `TOOL_CALL`,
  `TOOL_RESULT`, `MEMORY_APPEND`, `CRITIQUE`, `ESCALATION`, `CONSENSUS`,
  `CONTEXT_SYNC`. Each has a matching `emit_*` method and `on_*` decorator on
  `Dendrite`. Decorators accept `neuron=` / `capability=` / `trace_id=` filter
  kwargs and `on_trace(trace_id, *types)` narrows a handler to a single
  workflow.
- **Capability-routed dispatch**  -  `dispatch(capabilities=..., ...)` publishes
  on `cosmonapse.<ns>.TASK.routed` with a queue group keyed on each Dendrite's
  aggregate capabilities, so identical-cap-profile Dendrites load-balance and
  the broker delivers each TASK exactly once within the group.
- **Competitive bidding**  -  `dispatch_offer(input=..., capabilities=...,
  deadline_ms=..., select=...)` runs the `TASK_OFFER` / `BID` / `TASK_AWARDED`
  flow. Selection strategies: `"first_bid"`, `"lowest_cost"`,
  `"highest_confidence"`. Returns a Pathway scoped to the awarded workflow.
- **Dispatch sugar**  -  `dispatch_and_wait(...)` (dispatch, await first
  terminal signal, return it) and `dispatch_and_subscribe(...)` (dispatch and
  return the live Pathway).
- **CLI**  -  new `cosmo init` command scaffolds a minimal Axon + Dendrite
  project. New `cosmo completion` prints a bash/zsh/fish completion script.
  `cosmo synapse view` gained namespace listing and per-namespace signal
  streaming. Internal `_prism` / `_prism_view` / `_prism_hero` modules back the
  richer signal-tree rendering used by `synapse view` and `prism`.
- **TypeScript SDK** (`@cosmonapse/sdk`)  -  first published version (`0.1.0`).
  Envelope types and codec, typed signal builders, `Synapse` interface plus
  in-process `MemorySynapse` and networked `NatsSynapse`, `RegistryStore` with
  `MemoryRegistryStore`, `Neuron` / `Axon` / `Dendrite`, and the
  `expressNeuron` / `mcpNeuron` / unified `neuron()` factories. Parity gaps
  versus the Python SDK are tracked in `packages/ts-sdk/PORTING_STATUS.md`.
- `py.typed` marker so type checkers honour the package's annotations
  (PEP 561).
- Optional dependency extras: `[kafka]` (aiokafka), `[postgres]` (asyncpg), and
  `[flask]` (Flask/WSGI Neuron factory), alongside the existing `[nats]`.
- `ContextFetcher` is now exported from the top-level package and `__all__`.
- Top-level `README.md`, `CONTRIBUTING.md`, `CHANGELOG.md`, `LICENSE`, and
  `design/ENGRAM_DESIGN.md`.
- A real test suite covering storage backends, the in-memory and dev synapses,
  Dendrite protocol behaviour, the cognition API, event-driven flows, Pathway,
  Engram, and the Neuron factories. Adapter tests that require live
  infrastructure (NATS / Kafka / Postgres) skip automatically when it is
  unavailable.

### Changed
- `Dendrite` is now the canonical orchestrator type. `Cortex` remains as a
  back-compat alias; the public `Dendrite.cortex_id` attribute is gone  -  use
  `dendrite_id`. The role guard now sits on `emit()` itself, so every cognition
  emitter funnels through it and worker-role Dendrites are blocked from
  emitting orchestration signals (except `bid()`, which uses the private
  publish path because bidding is how workers participate in capability
  routing).
- Flask is no longer a core runtime dependency; it moved to the `[flask]`
  extra. `pip install cosmonapse` no longer pulls in Flask.
- `Dendrite.publish()` is now private (`_publish()`). All public outbound
  signals must go through `emit()`, which enforces the `SYNAPSE_TYPES` guard,
  so the protocol can no longer be bypassed accidentally.
- The `_signal`-suffixed handler decorators (`on_error_signal`, etc.) are now
  the documented canonical names.
- `_install.py` (PATH manipulation) moved from the `cosmonapse` SDK package
  into the `cosmo` CLI package, where shell-config changes belong.

### Deprecated
- The short handler aliases `on_error`, `on_register`, `on_deregister`, and
  `on_heartbeat` now emit a `DeprecationWarning`. Use the `_signal` forms.

### Removed
- The standalone `cosmo dev` command (the dev synapse is now managed through
  `cosmo synapse start memory`).
- Unused `anyio` core dependency.
- The deprecated `cosmonapse.transport` compatibility shim.
- The deprecat